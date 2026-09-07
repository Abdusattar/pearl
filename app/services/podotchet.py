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

from app.models import AccountBalanceSnapshot, CapitalWithdrawal, CashFunding, Transaction

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
            "source_founder_id": f.source_founder_id,
            "comment": f.comment,
        }
        for f in fundings
    ]


def get_podotchet_ledger(db: Session, organization_id: int) -> list[dict]:
    """Пополнения этого бизнеса с остатком (remaining) после списания расходов
    FIFO — от самого старого пополнения к новому. Изъятия учредителей
    (CapitalWithdrawal) уменьшают пул тем же образом, что расходы — деньги
    физически ушли из кассы, но это не Transaction/расход бизнеса."""
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
    withdrawn_capital = db.query(func.coalesce(func.sum(CapitalWithdrawal.amount), 0)).filter(
        CapitalWithdrawal.organization_id == organization_id,
        CapitalWithdrawal.date >= PODOTCHET_START_DATE,
        CapitalWithdrawal.deleted_at.is_(None),
    ).scalar()
    pool = Decimal(consumed) + Decimal(withdrawn_capital)
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

    # Доход, собранный наличными мимо счёта (оплата разовой услуги наличными,
    # app/services/service_payments.py), создаёт income-Transaction И
    # CashFunding(direct_cash, source_transaction_id) на ту же сумму — деньги уже
    # посчитаны в кассе, на счёт они не попадали. Без этого исключения счёт
    # задваивал бы такой доход (пойман 04.09, проверяя новый экран остатков).
    cash_income_txn_ids = db.query(CashFunding.source_transaction_id).filter(
        CashFunding.source_transaction_id.isnot(None), CashFunding.deleted_at.is_(None),
    ).scalar_subquery()

    income = db.query(func.coalesce(func.sum(Transaction.amount), 0)).filter(
        Transaction.organization_id == organization_id, Transaction.type == "income",
        Transaction.date > since, Transaction.date <= as_of, Transaction.deleted_at.is_(None),
        Transaction.id.notin_(cash_income_txn_ids),
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


def get_founder_capital(db: Session, organization_id: int) -> list[dict]:
    """Баланс капитала по каждому учредителю (внесено − изъято) для этого
    бизнеса — по совету финэксперта различаются по конкретному человеку, не
    общей строкой, иначе через время не свести, кто сколько реально вложил."""
    contributed: dict[int, Decimal] = {}
    rows = db.query(CashFunding).filter(
        CashFunding.organization_id == organization_id,
        CashFunding.source_founder_id.isnot(None),
        CashFunding.deleted_at.is_(None),
    ).all()
    for r in rows:
        contributed[r.source_founder_id] = contributed.get(r.source_founder_id, ZERO) + Decimal(r.amount)

    withdrawn: dict[int, Decimal] = {}
    rows = db.query(CapitalWithdrawal).filter(
        CapitalWithdrawal.organization_id == organization_id,
        CapitalWithdrawal.deleted_at.is_(None),
    ).all()
    for r in rows:
        withdrawn[r.founder_user_id] = withdrawn.get(r.founder_user_id, ZERO) + Decimal(r.amount)

    founder_ids = set(contributed) | set(withdrawn)
    return [
        {
            "founder_user_id": fid,
            "contributed": contributed.get(fid, ZERO),
            "withdrawn": withdrawn.get(fid, ZERO),
            "balance": contributed.get(fid, ZERO) - withdrawn.get(fid, ZERO),
        }
        for fid in founder_ids
    ]


def get_capital_movements(db: Session, organization_id: int) -> list[dict]:
    """Взносы и изъятия учредителей вместе, по дате — для истории на экране."""
    moves = []
    rows = db.query(CashFunding).filter(
        CashFunding.organization_id == organization_id,
        CashFunding.source_founder_id.isnot(None),
        CashFunding.deleted_at.is_(None),
    ).all()
    for r in rows:
        moves.append({
            "kind": "in", "id": r.id, "date": r.date, "amount": Decimal(r.amount),
            "founder_user_id": r.source_founder_id, "comment": r.comment,
        })
    rows = db.query(CapitalWithdrawal).filter(
        CapitalWithdrawal.organization_id == organization_id,
        CapitalWithdrawal.deleted_at.is_(None),
    ).all()
    for r in rows:
        moves.append({
            "kind": "out", "id": r.id, "date": r.date, "amount": Decimal(r.amount),
            "founder_user_id": r.founder_user_id, "comment": r.comment,
        })
    moves.sort(key=lambda m: (m["date"], m["id"]), reverse=True)
    return moves


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
