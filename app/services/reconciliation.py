"""Сверка остатков — счёт, касса объекта, долг поставщику (07.09).

Одна механика на все три вида: система считает, сколько должно быть
(`expected_for`), человек вводит, сколько есть на самом деле, и запись
сохраняет обе цифры вместе с разницей. Разница хранится, а не выводится
задним числом — в этом весь смысл: после сверки расхождение остаётся
фактом, который нельзя стереть следующей сверкой.

Склада здесь нет намеренно — там не одна цифра, а список продуктов,
и пересчёт уже пишет настоящие приход/списание (/warehouse/count/).
"""
from datetime import date as date_cls
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import Reconciliation
from app.services import podotchet, supplier_ledger

ZERO = Decimal("0")

ACCOUNT = "account"
CASH = "cash"
SUPPLIER_DEBT = "supplier_debt"

KIND_LABELS = {
    ACCOUNT: "Счёт в банке",
    CASH: "Касса садика",
    SUPPLIER_DEBT: "Долг поставщику",
}

# Расхождение до этого процента от ожидаемой суммы считается мелким (сдача,
# округление) — тон подсказки мягче. Выше порога либо больше BIG_DELTA в
# абсолюте — это уже разбирают отдельно. Проценты и сумма вынесены сюда, а не
# зашиты в шаблон: у школы и садика разный масштаб, а софт пойдёт и другим
# садикам (см. wiki — бизнес-правила настройками, не хардкодом).
SMALL_DELTA_PERCENT = Decimal("1")
BIG_DELTA = Decimal("5000")


def latest(db: Session, organization_id: int, kind: str,
           subject_id: int | None = None) -> Reconciliation | None:
    """Последняя действующая сверка этого вида (отменённые не в счёт)."""
    q = (
        db.query(Reconciliation)
        .filter(
            Reconciliation.organization_id == organization_id,
            Reconciliation.kind == kind,
            Reconciliation.cancelled_at.is_(None),
        )
    )
    if subject_id is not None:
        q = q.filter(Reconciliation.subject_id == subject_id)
    return q.order_by(Reconciliation.date.desc(), Reconciliation.id.desc()).first()


def history(db: Session, organization_id: int, limit: int = 20) -> list[Reconciliation]:
    return (
        db.query(Reconciliation)
        .filter(Reconciliation.organization_id == organization_id)
        .order_by(Reconciliation.date.desc(), Reconciliation.id.desc())
        .limit(limit)
        .all()
    )


def expected_cash(db: Session, organization_id: int) -> Decimal:
    """Сколько должно быть наличных в кассе объекта.

    Не `podotchet.get_org_balance()`: тот упирается в ноль, когда расходов
    больше, чем заведённых денег — перерасход при этом пропадает. Для сверки
    нужна честная величина, в том числе отрицательная: минус означает, что
    тратили из денег, которых в системе нет.

    Отсчёт идёт от предыдущей сверки кассы (09.09) — так каждая следующая
    сверка сравнивает факт не со всей историей объекта, а с тем, что реально
    случилось с кассой после прошлого пересчёта.
    """
    return podotchet.get_cash_state(db, organization_id)["net"]


def expected_for(db: Session, organization_id: int, kind: str,
                 subject_id: int | None = None,
                 as_of: date_cls | None = None) -> Decimal:
    """Сколько система думает, что должно быть — на момент сверки."""
    as_of = as_of or date_cls.today()
    if kind == ACCOUNT:
        return Decimal(podotchet.get_expected_balance(db, organization_id, as_of)["expected"])
    if kind == CASH:
        return expected_cash(db, organization_id)
    if kind == SUPPLIER_DEBT:
        if subject_id is None:
            raise ValueError("Для сверки долга нужен поставщик")
        return Decimal(supplier_ledger.get_supplier_balance(db, subject_id))
    raise ValueError(f"Неизвестный вид сверки: {kind}")


def severity(expected: Decimal, delta: Decimal) -> str:
    """match — сошлось, small — мелкое расхождение, big — заметное."""
    gap = abs(delta)
    if gap < Decimal("0.01"):
        return "match"
    base = max(abs(expected), Decimal("1"))
    if gap <= base * SMALL_DELTA_PERCENT / 100 and gap < BIG_DELTA:
        return "small"
    return "big"


def all_corrections(db: Session, limit: int = 200) -> list[Reconciliation]:
    """Реестр всех корректировок — касса, счёт, долги — по всем объектам.

    Отдельный список по прямому требованию заказчика (07.09): «все
    корректировки, что изменяют кассу, счёт, долги особенно» должны быть
    собраны в одном месте, «чтобы учредители могли проверить». Каждая правка
    остатка — событие, а не рутина, и оно должно быть на виду."""
    return (
        db.query(Reconciliation)
        .order_by(Reconciliation.created_at.desc(), Reconciliation.id.desc())
        .limit(limit)
        .all()
    )


def create(db: Session, *, organization_id: int, kind: str, actual: Decimal | float | str,
           user_id: int, on_date: date_cls | None = None,
           subject_id: int | None = None, reason: str = "") -> Reconciliation:
    """Записать сверку. expected считается здесь же и сохраняется вместе с
    разницей — восстановить его потом по базе было бы уже нельзя.

    `actual` нормализуется к Decimal на входе: форма отдаёт число как float
    (_parse_amount в роутере), а весь денежный слой считает в Decimal, и
    `float - Decimal` в Python — TypeError, а не молчаливое приведение.
    Из-за этого кнопка «Сохранить» на сверке падала с 500 (08.09)."""
    on_date = on_date or date_cls.today()
    actual = Decimal(str(actual))
    expected = expected_for(db, organization_id, kind, subject_id, on_date)
    rec = Reconciliation(
        organization_id=organization_id,
        kind=kind,
        subject_id=subject_id,
        date=on_date,
        expected_amount=expected,
        actual_amount=actual,
        delta=actual - expected,
        reason=reason.strip() or None,
        created_by=user_id,
    )
    db.add(rec)
    db.flush()
    return rec


def cancel(db: Session, rec_id: int, user_id: int, reason: str) -> Reconciliation | None:
    """Отменить сверку — вместо удаления. Строка остаётся видна с причиной
    отмены, иначе «исправление ошибки» ничем не отличалось бы от заметания."""
    rec = db.get(Reconciliation, rec_id)
    if not rec or rec.cancelled_at is not None:
        return None
    from sqlalchemy import func as sa_func
    rec.cancelled_at = sa_func.now()
    rec.cancelled_by = user_id
    rec.cancel_reason = reason.strip() or None
    db.flush()
    return rec
