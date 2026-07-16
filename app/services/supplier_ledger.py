"""Баланс поставщика — единственный источник правды про долг (16.07).

Платежи из ленты (SupplierPayment) не трогают Transaction.amount_paid — то поле
остаётся историческим фактом "сколько оплатили в момент закупа". Текущий долг
считается на лету: недоплаты по закупам + начальное сальдо, минус все платежи,
примененные по датам от старых долгов к новым (FIFO). Поэтому удаление/правка
платежа не требует отдельной логики отката — всё просто пересчитывается.
"""
from datetime import date as date_cls
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import ReceiptTransaction, Supplier, SupplierPayment, Transaction

ZERO = Decimal("0")


def _debt_buckets(db: Session, supplier_id: int) -> list[dict]:
    """Один бакет = один закуп со стороны поставщика (для FIFO/остатков по конкретной
    Transaction — используется /expenses для подсветки закупа). receipt_id проставлен
    там, где есть, чтобы get_ledger_rows мог схлопнуть разбивку по категориям одного
    чека в одну строку истории — иначе один визит к Айбеку выглядел бы как N закупов."""
    supplier = db.query(Supplier).get(supplier_id)
    buckets = []
    if supplier and supplier.opening_balance and supplier.opening_balance > ZERO:
        buckets.append({
            "kind": "opening",
            "date": supplier.opening_balance_date or (supplier.created_at.date() if supplier.created_at else None),
            "transaction_id": None,
            "receipt_id": None,
            "description": "Начальное сальдо",
            "original": Decimal(supplier.opening_balance),
        })

    txs = (
        db.query(Transaction)
        .filter(
            Transaction.supplier_id == supplier_id,
            Transaction.type == "expense",
            Transaction.deleted_at.is_(None),
        )
        .order_by(Transaction.date.asc(), Transaction.id.asc())
        .all()
    )
    tx_ids = [t.id for t in txs]
    receipt_by_tx = {}
    if tx_ids:
        receipt_by_tx = dict(
            db.query(ReceiptTransaction.transaction_id, ReceiptTransaction.receipt_id)
            .filter(ReceiptTransaction.transaction_id.in_(tx_ids))
            .all()
        )

    for t in txs:
        paid = t.amount_paid if t.amount_paid is not None else t.amount
        original = Decimal(t.amount) - Decimal(paid)
        if original > ZERO:
            buckets.append({
                "kind": "purchase",
                "date": t.date,
                "transaction_id": t.id,
                "receipt_id": receipt_by_tx.get(t.id),
                "description": t.description,
                "original": original,
            })

    buckets.sort(key=lambda b: (b["date"] or date_cls.min, b["kind"] != "opening"))
    return buckets


def get_supplier_ledger(db: Session, supplier_id: int) -> list[dict]:
    """Бакеты долга (opening + недоплаченные закупы), каждый с remaining после применения
    всех платежей FIFO по дате (от самого старого долга к новому)."""
    buckets = _debt_buckets(db, supplier_id)
    total_payments = db.query(func.coalesce(func.sum(SupplierPayment.amount), 0)).filter(
        SupplierPayment.supplier_id == supplier_id,
        SupplierPayment.deleted_at.is_(None),
    ).scalar()
    pool = Decimal(total_payments)
    for b in buckets:
        applied = min(b["original"], pool)
        b["remaining"] = b["original"] - applied
        pool -= applied
    return buckets


def get_supplier_balance(db: Session, supplier_id: int) -> Decimal:
    return sum((b["remaining"] for b in get_supplier_ledger(db, supplier_id)), ZERO)


def get_all_supplier_balances(db: Session) -> dict[int, Decimal]:
    """Баланс по каждому поставщику, у которого вообще есть долговые бакеты или платежи."""
    supplier_ids = {sid for (sid,) in db.query(Supplier.id).all()}
    return {sid: get_supplier_balance(db, sid) for sid in supplier_ids}


def get_transaction_remaining_debt(db: Session, supplier_id: int) -> dict[int, Decimal]:
    """transaction_id -> остаток долга по этому закупу с учётом уже сделанных платежей.
    Используется в /expenses для подсветки — иначе после погашения долга платежом
    старая недоплата продолжала бы висеть оранжевой меткой."""
    return {
        b["transaction_id"]: b["remaining"]
        for b in get_supplier_ledger(db, supplier_id)
        if b["kind"] == "purchase"
    }


def get_ledger_rows(db: Session, supplier_id: int) -> list[dict]:
    """Единая лента долгов и платежей поставщика, по дате — новые сверху (см. billing.get_ledger,
    тот же паттерн для детей). Закупы одного чека/записи (create_split_transactions режет их
    по категориям расходов) схлопнуты в одну строку — сотруднику и владельцу нужен один
    визит к поставщику, а не N технических проводок."""
    buckets = _debt_buckets(db, supplier_id)
    payments = (
        db.query(SupplierPayment)
        .filter(SupplierPayment.supplier_id == supplier_id, SupplierPayment.deleted_at.is_(None))
        .all()
    )

    rows = [
        {"date": b["date"], "amount": b["original"], "description": b["description"], "kind": "opening", "transaction_id": None}
        for b in buckets if b["kind"] == "opening"
    ]

    purchase_groups: dict = {}
    group_order: list = []
    for b in buckets:
        if b["kind"] != "purchase":
            continue
        key = b["receipt_id"] if b["receipt_id"] is not None else ("tx", b["transaction_id"])
        if key not in purchase_groups:
            purchase_groups[key] = {"date": b["date"], "amount": ZERO, "count": 0, "description": b["description"]}
            group_order.append(key)
        g = purchase_groups[key]
        g["amount"] += b["original"]
        g["count"] += 1
        if b["date"] and (not g["date"] or b["date"] < g["date"]):
            g["date"] = b["date"]
    for key in group_order:
        g = purchase_groups[key]
        rows.append({
            "date": g["date"], "amount": g["amount"],
            "description": g["description"] if g["count"] == 1 else f"{g['count']} категории",
            "kind": "purchase", "transaction_id": None,
        })

    rows += [
        {
            "date": p.date, "amount": p.amount, "description": p.comment,
            "kind": "payment", "payment_id": p.id,
        }
        for p in payments
    ]
    rows.sort(key=lambda r: r["date"] or date_cls.min, reverse=True)
    return rows


def add_payment(db: Session, supplier_id: int, amount: Decimal, payment_date, comment: str | None, user_id: int) -> SupplierPayment:
    balance = get_supplier_balance(db, supplier_id)
    if amount <= ZERO:
        raise ValueError("Сумма платежа должна быть больше нуля")
    if amount > balance:
        raise ValueError(f"Платёж ({amount} с) больше текущего долга ({balance} с)")
    payment = SupplierPayment(
        supplier_id=supplier_id, amount=amount, date=payment_date,
        comment=comment, created_by=user_id,
    )
    db.add(payment)
    return payment
