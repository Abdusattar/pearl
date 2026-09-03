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
# Копейки (тыйын) в обороте фактически не участвуют — остаток долга меньше 1 сома
# не является реальным долгом (округление/копеечный хвост от ручного ввода), а не
# то, что кто-то реально должен вернуть.
DUST = Decimal("1")


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
        if original >= DUST:
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


def _bulk_ledger_buckets(db: Session, supplier_ids: list[int]) -> dict[int, list[dict]]:
    """Общая батч-загрузка для get_transaction_remaining_debt_bulk и
    get_supplier_balances_bulk — 3 запроса суммарно на N поставщиков вместо
    3-4 запросов на каждого (список /expenses и его итог "Долг поставщикам"
    раньше дёргали функции по одному, с ~7+ поставщиками заметный N+1, 03.09).
    Логика бакетов — точная копия _debt_buckets + get_supplier_ledger, только
    данные читаются одним запросом на таблицу вместо одного на поставщика."""
    supplier_ids = list(set(supplier_ids))
    if not supplier_ids:
        return {}

    suppliers = {
        s.id: s for s in db.query(Supplier).filter(Supplier.id.in_(supplier_ids)).all()
    }

    txs = (
        db.query(Transaction)
        .filter(
            Transaction.supplier_id.in_(supplier_ids),
            Transaction.type == "expense",
            Transaction.deleted_at.is_(None),
        )
        .order_by(Transaction.date.asc(), Transaction.id.asc())
        .all()
    )
    txs_by_supplier: dict[int, list] = {}
    for t in txs:
        txs_by_supplier.setdefault(t.supplier_id, []).append(t)

    payments_by_supplier = dict(
        db.query(SupplierPayment.supplier_id, func.coalesce(func.sum(SupplierPayment.amount), 0))
        .filter(SupplierPayment.supplier_id.in_(supplier_ids), SupplierPayment.deleted_at.is_(None))
        .group_by(SupplierPayment.supplier_id)
        .all()
    )

    result: dict[int, list[dict]] = {}
    for sid in supplier_ids:
        buckets = []
        supplier = suppliers.get(sid)
        if supplier and supplier.opening_balance and supplier.opening_balance > ZERO:
            buckets.append({
                "kind": "opening",
                "date": supplier.opening_balance_date or (supplier.created_at.date() if supplier.created_at else None),
                "transaction_id": None,
                "original": Decimal(supplier.opening_balance),
            })
        for t in txs_by_supplier.get(sid, []):
            paid = t.amount_paid if t.amount_paid is not None else t.amount
            original = Decimal(t.amount) - Decimal(paid)
            if original >= DUST:
                buckets.append({
                    "kind": "purchase",
                    "date": t.date,
                    "transaction_id": t.id,
                    "original": original,
                })
        buckets.sort(key=lambda b: (b["date"] or date_cls.min, b["kind"] != "opening"))

        pool = Decimal(payments_by_supplier.get(sid, 0))
        for b in buckets:
            applied = min(b["original"], pool)
            b["remaining"] = b["original"] - applied
            pool -= applied
        result[sid] = buckets
    return result


def get_transaction_remaining_debt_bulk(
    db: Session, supplier_ids: list[int]
) -> dict[int, dict[int, Decimal]]:
    """Батч-версия get_transaction_remaining_debt для списка поставщиков разом."""
    buckets_by_supplier = _bulk_ledger_buckets(db, supplier_ids)
    return {
        sid: {b["transaction_id"]: b["remaining"] for b in buckets if b["kind"] == "purchase"}
        for sid, buckets in buckets_by_supplier.items()
    }


def get_supplier_balances_bulk(db: Session, supplier_ids: list[int]) -> dict[int, Decimal]:
    """Батч-версия get_supplier_balance для списка поставщиков разом."""
    buckets_by_supplier = _bulk_ledger_buckets(db, supplier_ids)
    return {
        sid: sum((b["remaining"] for b in buckets), ZERO)
        for sid, buckets in buckets_by_supplier.items()
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
