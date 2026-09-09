"""Наличные из кассы одного бизнеса в кассу другого (09.09).

«Одолжили у другого бизнеса» была односторонней записью: касса получателя
росла, касса донора не уменьшалась. Пока это касалось Кожомкула, чья касса не
велась, дыра не всплывала. Реальный случай — садик снимает 260 000 со счёта и
передаёт школе на стройматериалы: до правки система показала бы эти деньги
разом в двух кассах.

Отдельной таблицы нет: строка пополнения у получателя (source_organization_id
= донор) и есть перевод, донор вычитает её у себя.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from app.models import CashFunding, Organization, Reconciliation, Transaction, User
from app.services import podotchet

START = podotchet.PODOTCHET_START_DATE


@pytest.fixture()
def orgs(db):
    """Два бизнеса со своими держателями касс — садик (донор) и школа."""
    made = []
    for title in ("Садик Сокулук", "Школа"):
        holder = User(name=f"Держатель {title}", role="staff")
        db.add(holder)
        db.flush()
        o = Organization(name=title, type="садик", cash_recipient_user_id=holder.id)
        db.add(o)
        db.flush()
        o._holder_id = holder.id
        made.append(o)
    return made


def _fund(db, org, amount, on_date, created_at=None, source_org=None):
    f = CashFunding(
        organization_id=org.id,
        source_type="direct_cash" if source_org else "withdrawal",
        amount=Decimal(amount), date=on_date,
        taken_by=org._holder_id, accountable_user_id=org._holder_id,
        source_organization_id=source_org.id if source_org else None,
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


def test_transfer_moves_cash_out_of_the_donor(db, orgs):
    """Случай школы: садик снял 260 000 и передал их школе.

    Деньги должны оказаться в одной кассе, а не показаться в двух.
    """
    sadik, school = orgs
    _fund(db, sadik, "260000", START + timedelta(days=1))
    _fund(db, school, "260000", START + timedelta(days=2), source_org=sadik)

    assert podotchet.get_org_balance(db, sadik.id) == Decimal("0")
    assert podotchet.get_org_balance(db, school.id) == Decimal("260000")


def test_donor_keeps_the_rest_of_its_cash(db, orgs):
    """Отдали часть — остальное у донора остаётся."""
    sadik, school = orgs
    _fund(db, sadik, "300000", START + timedelta(days=1))
    _fund(db, school, "260000", START + timedelta(days=2), source_org=sadik)

    assert podotchet.get_org_balance(db, sadik.id) == Decimal("40000")


def test_receiver_spends_transferred_money(db, orgs):
    """Школа закупает стройматериалы из переданных денег."""
    sadik, school = orgs
    _fund(db, sadik, "260000", START + timedelta(days=1))
    _fund(db, school, "260000", START + timedelta(days=2), source_org=sadik)
    _spend(db, school, "180000", START + timedelta(days=3))

    assert podotchet.get_org_balance(db, school.id) == Decimal("80000")
    assert podotchet.get_org_balance(db, sadik.id) == Decimal("0")


def test_transfer_beyond_own_cash_shows_as_uncovered(db, orgs):
    """Отдали больше, чем есть — перерасход виден, а не прячется в нуле.

    Ровно то же поведение, что у расходов сверх заведённых денег: минус
    означает, что тратили из того, чего в системе нет.
    """
    sadik, school = orgs
    _fund(db, sadik, "100000", START + timedelta(days=1))
    _fund(db, school, "260000", START + timedelta(days=2), source_org=sadik)

    assert podotchet.get_org_balance(db, sadik.id) == Decimal("0")
    assert podotchet.get_uncovered_expenses(db, sadik.id) == Decimal("160000")


def test_deleted_transfer_returns_cash_to_the_donor(db, orgs):
    """Одна строка — два конца: удалили перевод, откатились оба."""
    sadik, school = orgs
    _fund(db, sadik, "260000", START + timedelta(days=1))
    transfer = _fund(db, school, "260000", START + timedelta(days=2), source_org=sadik)

    assert podotchet.get_org_balance(db, sadik.id) == Decimal("0")

    transfer.deleted_at = datetime(2026, 9, 10, 12, 0)
    db.flush()

    assert podotchet.get_org_balance(db, sadik.id) == Decimal("260000")
    assert podotchet.get_org_balance(db, school.id) == Decimal("0")


def test_transfer_before_reconciliation_is_already_inside(db, orgs):
    """Перевод, сделанный до сверки кассы донора, второй раз её не уменьшает."""
    sadik, school = orgs
    _fund(db, sadik, "260000", date(2026, 9, 1))
    _fund(db, school, "260000", date(2026, 9, 2), source_org=sadik)
    db.add(Reconciliation(
        organization_id=sadik.id, kind="cash", date=date(2026, 9, 8),
        expected_amount=Decimal("0"), actual_amount=Decimal("3500"),
        delta=Decimal("0"), created_at=datetime(2026, 9, 8, 11, 47),
    ))
    db.flush()

    assert podotchet.get_org_balance(db, sadik.id) == Decimal("3500")


def test_transfer_after_reconciliation_reduces_the_new_baseline(db, orgs):
    """Перевод после сверки — уменьшает пересчитанную кассу."""
    sadik, school = orgs
    db.add(Reconciliation(
        organization_id=sadik.id, kind="cash", date=date(2026, 9, 8),
        expected_amount=Decimal("0"), actual_amount=Decimal("50000"),
        delta=Decimal("0"), created_at=datetime(2026, 9, 8, 11, 47),
    ))
    db.flush()
    _fund(db, school, "20000", date(2026, 9, 9), source_org=sadik)

    assert podotchet.get_org_balance(db, sadik.id) == Decimal("30000")
    assert podotchet.get_org_balance(db, school.id) == Decimal("20000")


def _make_owner(db, org):
    import bcrypt
    u = User(
        name="__test_transfer_owner__", role="owner", organization_id=org.id,
        password_hash=bcrypt.hashpw(b"test-pass", bcrypt.gensalt()).decode(),
    )
    db.add(u)
    db.flush()
    return u


def _post_transfer(client, db, current, other, direction, amount="260000"):
    user = _make_owner(db, current)
    client.post("/login", data={"user_id": user.id, "password": "test-pass"})
    client.post("/podotchet/transfer", data={
        "amount": amount, "date": "2026-09-09",
        "other_organization_id": str(other.id), "direction": direction,
        "comment": "на стройматериалы школы", "org_id": str(current.id),
    }, follow_redirects=False)


def test_podotchet_page_renders_transfer_form(client, db, orgs):
    """Страница подотчёта открывается и показывает форму перетока.

    Форма собирает имена держателей касс всех бизнесов (holder_names) и
    подставляет их в скрипт — ошибка в шаблоне уронила бы весь экран, а не
    только модалку.
    """
    sadik, school = orgs
    user = _make_owner(db, sadik)
    client.post("/login", data={"user_id": user.id, "password": "test-pass"})

    page = client.get(f"/podotchet/?org_id={sadik.id}")
    assert page.status_code == 200
    assert "Переток между бизнесами" in page.text
    assert "Передали в другой бизнес" in page.text
    assert "Получили из другого бизнеса" in page.text


def test_route_records_incoming_transfer(client, db, orgs):
    """Заводим со страницы школы: «получили из садика»."""
    sadik, school = orgs
    _fund(db, sadik, "260000", START + timedelta(days=1))
    _post_transfer(client, db, school, sadik, "in")

    assert podotchet.get_org_balance(db, school.id) == Decimal("260000")
    assert podotchet.get_org_balance(db, sadik.id) == Decimal("0")


def test_route_records_outgoing_transfer(client, db, orgs):
    """Та же операция со страницы садика: «передали в школу».

    Направление меняет концы, но не тип записи — результат обязан совпасть
    с входящим вариантом, иначе одна и та же передача денег считалась бы
    по-разному в зависимости от того, с чьей страницы её завели.
    """
    sadik, school = orgs
    _fund(db, sadik, "260000", START + timedelta(days=1))
    _post_transfer(client, db, sadik, school, "out")

    assert podotchet.get_org_balance(db, school.id) == Decimal("260000")
    assert podotchet.get_org_balance(db, sadik.id) == Decimal("0")

    saved = db.query(CashFunding).filter(CashFunding.source_organization_id == sadik.id).one()
    assert saved.organization_id == school.id
    # Отчитывается держатель кассы получателя, а не того, с чьей страницы завели
    assert saved.accountable_user_id == school._holder_id


def test_flows_panel_still_shows_the_transfer(db, orgs):
    """Витрина «Перетоки между бизнесами» не сломалась — направление и сумма."""
    sadik, school = orgs
    _fund(db, school, "260000", START + timedelta(days=2), source_org=sadik)

    flows = podotchet.get_cross_org_flows(db, START, START + timedelta(days=30))
    assert flows == [{"from_org_id": sadik.id, "to_org_id": school.id,
                      "amount": Decimal("260000")}]
