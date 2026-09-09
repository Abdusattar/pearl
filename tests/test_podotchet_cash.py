"""Касса объекта: сверка кассы как точка отсчёта (09.09).

До 09.09 сверка кассы только записывала расхождение — остаток она не двигала,
в отличие от сверки счёта. Махабат ввела «в кассе 3 500», а подотчёт продолжал
показывать 0, потому что FIFO упирался в ноль. Тесты фиксируют новое поведение
и границу «что уже внутри пересчитанной суммы, а что ещё нет».
"""
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from app.models import (
    CashFunding, Organization, Reconciliation, Transaction, User,
)
from app.services import podotchet, reconciliation

START = podotchet.PODOTCHET_START_DATE


@pytest.fixture()
def org(db):
    holder = User(name="Держатель кассы", role="staff")
    db.add(holder)
    db.flush()
    o = Organization(name="Тестовый объект", type="садик", cash_recipient_user_id=holder.id)
    db.add(o)
    db.flush()
    o._holder_id = holder.id
    return o


def _fund(db, org, amount, on_date, created_at=None):
    f = CashFunding(
        organization_id=org.id, source_type="withdrawal", amount=Decimal(amount),
        date=on_date, taken_by=org._holder_id, accountable_user_id=org._holder_id,
        created_at=created_at or datetime(2000, 1, 1),
    )
    db.add(f)
    db.flush()
    return f


def _spend(db, org, amount, on_date, created_at=None):
    t = Transaction(
        organization_id=org.id, type="expense", amount=Decimal(amount),
        date=on_date, paid_directly=False, created_at=created_at or datetime(2000, 1, 1),
    )
    db.add(t)
    db.flush()
    return t


def _reconcile(db, org, actual, on_date, created_at):
    r = Reconciliation(
        organization_id=org.id, kind="cash", date=on_date,
        expected_amount=Decimal("0"), actual_amount=Decimal(actual),
        delta=Decimal("0"), created_at=created_at,
    )
    db.add(r)
    db.flush()
    return r


def test_without_reconciliation_behaves_as_before(db, org):
    """Пока кассу не сверяли — прежний расчёт: пополнения минус расходы."""
    _fund(db, org, "10000", START + timedelta(days=1))
    _spend(db, org, "4000", START + timedelta(days=2))

    state = podotchet.get_cash_state(db, org.id)
    assert state["baseline"]["date"] is None
    assert state["net"] == Decimal("6000")
    assert state["on_hand"] == Decimal("6000")


def test_reconciliation_becomes_the_new_baseline(db, org):
    """Главный случай Махабат: расходов больше, чем заведено, FIFO даёт 0 —
    после сверки касса показывает введённую цифру, а не ноль."""
    _fund(db, org, "88000", date(2026, 9, 3))
    _spend(db, org, "122776", date(2026, 9, 4))

    assert podotchet.get_org_balance(db, org.id) == Decimal("0")
    assert podotchet.get_uncovered_expenses(db, org.id) == Decimal("34776")

    _reconcile(db, org, "3500", date(2026, 9, 8), datetime(2026, 9, 8, 11, 47))

    state = podotchet.get_cash_state(db, org.id)
    assert state["on_hand"] == Decimal("3500")
    assert state["net"] == Decimal("3500")
    # Перерасход поглощён сверкой — он больше не висит
    assert podotchet.get_uncovered_expenses(db, org.id) == Decimal("0")


def test_movements_before_reconciliation_are_not_counted_twice(db, org):
    """Всё, что было до сверки, уже внутри пересчитанной суммы."""
    _fund(db, org, "50000", date(2026, 9, 1))
    _spend(db, org, "20000", date(2026, 9, 2))
    _reconcile(db, org, "3500", date(2026, 9, 8), datetime(2026, 9, 8, 11, 47))

    assert podotchet.get_cash_state(db, org.id)["on_hand"] == Decimal("3500")


def test_movements_after_reconciliation_are_added(db, org):
    _reconcile(db, org, "3500", date(2026, 9, 8), datetime(2026, 9, 8, 11, 47))
    _fund(db, org, "10000", date(2026, 9, 9))
    _spend(db, org, "2000", date(2026, 9, 9))

    assert podotchet.get_cash_state(db, org.id)["on_hand"] == Decimal("11500")


def test_same_day_expense_entered_after_reconciliation_counts(db, org):
    """Пересчитали кассу в 11:47, купили продукты в 15:00 того же дня.

    По одной дате эти два события не различить — граница берётся по времени
    занесения записи. Без этого дневной расход молча терялся бы, а касса
    показывала бы больше денег, чем есть."""
    _reconcile(db, org, "3500", date(2026, 9, 8), datetime(2026, 9, 8, 11, 47))
    _spend(db, org, "1200", date(2026, 9, 8), created_at=datetime(2026, 9, 8, 15, 0))

    assert podotchet.get_cash_state(db, org.id)["on_hand"] == Decimal("2300")


def test_same_day_expense_entered_before_reconciliation_is_ignored(db, org):
    """Зеркальный случай: расход занесли до пересчёта — он уже в 3 500."""
    _spend(db, org, "1200", date(2026, 9, 8), created_at=datetime(2026, 9, 8, 9, 0))
    _reconcile(db, org, "3500", date(2026, 9, 8), datetime(2026, 9, 8, 11, 47))

    assert podotchet.get_cash_state(db, org.id)["on_hand"] == Decimal("3500")


def test_cancelled_reconciliation_is_ignored(db, org):
    """Отменённая сверка не может остаться базой расчёта."""
    _fund(db, org, "10000", date(2026, 9, 1))
    rec = _reconcile(db, org, "3500", date(2026, 9, 8), datetime(2026, 9, 8, 11, 47))
    rec.cancelled_at = datetime(2026, 9, 9, 10, 0)
    db.flush()

    state = podotchet.get_cash_state(db, org.id)
    assert state["baseline"]["date"] is None
    assert state["on_hand"] == Decimal("10000")


def test_latest_reconciliation_wins(db, org):
    _reconcile(db, org, "3500", date(2026, 9, 8), datetime(2026, 9, 8, 11, 47))
    _reconcile(db, org, "9000", date(2026, 9, 9), datetime(2026, 9, 9, 10, 0))

    assert podotchet.get_cash_state(db, org.id)["on_hand"] == Decimal("9000")


def test_baseline_goes_to_the_cash_holder(db, org):
    """Пересчитанная касса принадлежит объекту, отчитывается за неё держатель."""
    _reconcile(db, org, "3500", date(2026, 9, 8), datetime(2026, 9, 8, 11, 47))

    balances = podotchet.get_balances_by_person(db, org.id)
    assert balances == {org._holder_id: Decimal("3500")}


def test_expenses_eat_the_baseline_before_new_fundings(db, org):
    """База — самые старые деньги: расход съедает сначала её."""
    _reconcile(db, org, "3500", date(2026, 9, 8), datetime(2026, 9, 8, 11, 47))
    _fund(db, org, "10000", date(2026, 9, 9))
    _spend(db, org, "5000", date(2026, 9, 10))

    state = podotchet.get_cash_state(db, org.id)
    assert state["baseline_remaining"] == Decimal("0")
    assert state["buckets"][0]["remaining"] == Decimal("8500")
    assert state["on_hand"] == Decimal("8500")


def test_next_reconciliation_compares_against_baseline(db, org):
    """Следующая сверка сравнивает факт с тем, что случилось после прошлой,
    а не со всей историей объекта."""
    _reconcile(db, org, "3500", date(2026, 9, 8), datetime(2026, 9, 8, 11, 47))
    _spend(db, org, "500", date(2026, 9, 9))

    assert reconciliation.expected_cash(db, org.id) == Decimal("3000")


def test_overspend_after_reconciliation_shows_again(db, org):
    """Перерасход не исчезает навсегда — он снова копится после сверки."""
    _reconcile(db, org, "3500", date(2026, 9, 8), datetime(2026, 9, 8, 11, 47))
    _spend(db, org, "5000", date(2026, 9, 9))

    assert podotchet.get_org_balance(db, org.id) == Decimal("0")
    assert podotchet.get_uncovered_expenses(db, org.id) == Decimal("1500")


# --- Защита от двойного сабмита на форме сверки -----------------------------

def _make_owner(db, org):
    import bcrypt
    u = User(
        name="__test_reconcile_owner__", role="owner", organization_id=org.id,
        password_hash=bcrypt.hashpw(b"test-pass", bcrypt.gensalt()).decode(),
    )
    db.add(u)
    db.flush()
    return u


def test_double_submit_of_reconciliation_saves_once(client, db, org):
    """08.09 сверка кассы записалась дважды за 44 секунды — реестр
    корректировок, который смотрят собственники, задваивался."""
    user = _make_owner(db, org)
    client.post("/login", data={"user_id": user.id, "password": "test-pass"})

    payload = {
        "balance": "3500", "date": "2026-09-08", "organization_id": str(org.id),
        "kind": "cash", "comment": "пересчитали кассу", "org_id": str(org.id),
    }
    for _ in range(3):
        client.post("/podotchet/reconcile", data=payload, follow_redirects=False)

    saved = db.query(Reconciliation).filter(
        Reconciliation.organization_id == org.id, Reconciliation.kind == "cash",
    ).all()
    assert len(saved) == 1
    assert saved[0].actual_amount == Decimal("3500")
