"""Подотчёт — деньги в руках сотрудников (25.08).

Пополнение (снятие со счёта либо наличными напрямую, минуя банк) открывает
"бакет" в пуле подотчёта того бизнеса (organization_id). Расходы
(Transaction.paid_directly=False, по умолчанию) списываются с этого пула
сами, FIFO по дате от старого пополнения к новому — тот же приём, что
app/services/supplier_ledger.py (там пул погашается платежами, здесь —
пополняется ими, а расходуется тратами). Ничего не хранится — считается
на лету при каждом обращении.

Расчётный остаток по банковскому счёту — отдельная функция: последняя
введённая точка сверки (AccountBalanceSnapshot) + приход − снятия (только
source_type='withdrawal') − прямые расходы (paid_directly=True) после неё.
Наличные, собранные напрямую (source_type='direct_cash'), в этот расчёт не
входят вообще — этих денег на счету никогда не было.
"""
from datetime import date as date_cls
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import AccountBalanceSnapshot, CashFunding, Transaction

ZERO = Decimal("0")
# Копейки в обороте не считаются реальным остатком "на руках" — тот же порог,
# что и в supplier_ledger.py, по той же причине (округление/копеечный хвост).
DUST = Decimal("1")

# Подотчёт начинает считаться с этой даты, не раньше (25.08.2026, по аналогии
# с billing_cutoff/legacy_tariff — чистый старт, не пытаемся развести по
# снятиям задним числом реальные расходы, введённые в систему до появления
# самого механизма). Без этой границы старые Transaction (paid_directly=False
# по умолчанию, т.к. колонка добавлена этой же миграцией) мгновенно "съедали"
# бы любое новое пополнение, хотя реального снятия под них никогда не было —
# поймано на живом тесте, до деплоя.
PODOTCHET_START_DATE = date_cls(2026, 8, 25)


def _funding_buckets(db: Session, organization_id: int) -> list[dict]:
    fundings = (
        db.query(CashFunding)
        .filter(CashFunding.organization_id == organization_id, CashFunding.deleted_at.is_(None))
        .order_by(CashFunding.date.asc(), CashFunding.id.asc())
        .all()
    )
    return [
        {
            "id": f.id, "date": f.date, "amount": Decimal(f.amount),
            "source_type": f.source_type, "taken_by": f.taken_by,
            "accountable_user_id": f.accountable_user_id,
            "source_organization_id": f.source_organization_id,
            "comment": f.comment,
        }
        for f in fundings
    ]


def get_podotchet_ledger(db: Session, organization_id: int) -> list[dict]:
    """Пополнения этого бизнеса с остатком (remaining) после списания расходов
    FIFO — от самого старого пополнения к новому."""
    buckets = _funding_buckets(db, organization_id)
    consumed = db.query(func.coalesce(func.sum(
        func.coalesce(Transaction.amount_paid, Transaction.amount)
    ), 0)).filter(
        Transaction.organization_id == organization_id,
        Transaction.type == "expense",
        Transaction.paid_directly.is_(False),
        Transaction.date >= PODOTCHET_START_DATE,
        Transaction.deleted_at.is_(None),
    ).scalar()
    pool = Decimal(consumed)
    for b in buckets:
        applied = min(b["amount"], pool)
        b["remaining"] = b["amount"] - applied
        pool -= applied
    return buckets


def get_org_balance(db: Session, organization_id: int) -> Decimal:
    return sum((b["remaining"] for b in get_podotchet_ledger(db, organization_id)), ZERO)


def get_balances_by_person(db: Session, organization_id: int) -> dict[int, Decimal]:
    """user_id -> сколько сейчас на руках (по остаткам его пополнений, после FIFO)."""
    result: dict[int, Decimal] = {}
    for b in get_podotchet_ledger(db, organization_id):
        if b["remaining"] > DUST:
            result[b["accountable_user_id"]] = result.get(b["accountable_user_id"], ZERO) + b["remaining"]
    return result


def get_latest_snapshot(db: Session, organization_id: int, as_of: date_cls) -> AccountBalanceSnapshot | None:
    return (
        db.query(AccountBalanceSnapshot)
        .filter(AccountBalanceSnapshot.organization_id == organization_id, AccountBalanceSnapshot.date <= as_of)
        .order_by(AccountBalanceSnapshot.date.desc(), AccountBalanceSnapshot.id.desc())
        .first()
    )


def get_expected_balance(db: Session, organization_id: int, as_of: date_cls) -> dict:
    """Расчётный остаток по счёту на дату as_of + раскладка, откуда цифра.
    Только withdrawal-снятия и прямые расходы трогают счёт — наличные напрямую
    (direct_cash) в расчёт не входят: этих денег на счету никогда не было."""
    snapshot = get_latest_snapshot(db, organization_id, as_of)
    base = Decimal(snapshot.balance) if snapshot else ZERO
    since = snapshot.date if snapshot else date_cls.min

    income = db.query(func.coalesce(func.sum(Transaction.amount), 0)).filter(
        Transaction.organization_id == organization_id, Transaction.type == "income",
        Transaction.date > since, Transaction.date <= as_of, Transaction.deleted_at.is_(None),
    ).scalar()

    withdrawals = db.query(func.coalesce(func.sum(CashFunding.amount), 0)).filter(
        CashFunding.organization_id == organization_id, CashFunding.source_type == "withdrawal",
        CashFunding.date > since, CashFunding.date <= as_of, CashFunding.deleted_at.is_(None),
    ).scalar()

    direct = db.query(func.coalesce(func.sum(
        func.coalesce(Transaction.amount_paid, Transaction.amount)
    ), 0)).filter(
        Transaction.organization_id == organization_id, Transaction.type == "expense",
        Transaction.paid_directly.is_(True),
        Transaction.date > since, Transaction.date <= as_of, Transaction.deleted_at.is_(None),
    ).scalar()

    income, withdrawals, direct = Decimal(income), Decimal(withdrawals), Decimal(direct)
    return {
        "expected": base + income - withdrawals - direct,
        "base": base, "since": snapshot.date if snapshot else None,
        "income": income, "withdrawals": withdrawals, "direct": direct,
    }


def get_cross_org_flows(db: Session, since: date_cls, until: date_cls) -> list[dict]:
    """Пополнения, где реальный физический источник денег — другой бизнес
    (source_organization_id заполнен), сгруппированные (откуда -> куда) за
    период. Справочно — без формального долга/погашения (25.08)."""
    rows = (
        db.query(CashFunding)
        .filter(
            CashFunding.source_organization_id.isnot(None),
            CashFunding.date >= since, CashFunding.date <= until,
            CashFunding.deleted_at.is_(None),
        )
        .all()
    )
    grouped: dict[tuple[int, int], Decimal] = {}
    for r in rows:
        key = (r.source_organization_id, r.organization_id)
        grouped[key] = grouped.get(key, ZERO) + Decimal(r.amount)
    return [{"from_org_id": k[0], "to_org_id": k[1], "amount": v} for k, v in grouped.items()]


def get_spend_by_category(db: Session, organization_id: int, since: date_cls, until: date_cls) -> list[dict]:
    """Расходы за период по категориям — общая картина "куда ушло", не зависит
    от того, из подотчёта они или напрямую."""
    from app.models import ExpenseCategory
    rows = (
        db.query(ExpenseCategory.name, func.sum(Transaction.amount))
        .join(ExpenseCategory, ExpenseCategory.id == Transaction.category_id)
        .filter(
            Transaction.organization_id == organization_id, Transaction.type == "expense",
            Transaction.date >= since, Transaction.date <= until, Transaction.deleted_at.is_(None),
        )
        .group_by(ExpenseCategory.name)
        .order_by(func.sum(Transaction.amount).desc())
        .all()
    )
    return [{"category": name, "amount": Decimal(total)} for name, total in rows]
