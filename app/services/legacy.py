"""Записи старого входа в новой карточке (блок «Расходы», 21.09).

До 16.09 покупки и расходы писались без шапки `Purchase`: одна квитанция
(`Receipt` + `ReceiptTransaction`) или одна голая проводка. В ленте сентября
таких строк ~60, и они открывались в старом входе, часть на 404. Здесь они
собираются в то же, что видит карточка покупки: у кого, когда, позиции, как
оплачено, кто завёл. «Поправить» пересоздаёт запись через «Купили» или
«Расход без чека» с временем исходной записи (`keep_entry_time`), «Убрать» —
мягко, как у новых покупок.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import (Receipt, ReceiptItem, ReceiptTransaction, Supplier, Transaction, User,
                        WarehouseReceipt)
from app.services.price_check import fmt_money
from app.services.purchases import _fmt_num, _row_dict, audit, site_orgs


def txs(db: Session, site_org_id: int, kind: str, item_id: int) -> list[Transaction]:
    """Живые проводки записи старого входа: `r` — квитанция, `t` — одна проводка."""
    orgs = {o.id for o in site_orgs(db, site_org_id)} | {site_org_id}
    if kind == "r":
        ids = [r[0] for r in db.query(ReceiptTransaction.transaction_id)
               .filter(ReceiptTransaction.receipt_id == item_id).all()]
        q = db.query(Transaction).filter(Transaction.id.in_(ids)) if ids else None
    elif kind == "t":
        q = db.query(Transaction).filter(Transaction.id == item_id)
    else:
        return []
    if q is None:
        return []
    out = q.filter(Transaction.deleted_at.is_(None), Transaction.purchase_id.is_(None),
                   Transaction.type == "expense").order_by(Transaction.id).all()
    return [t for t in out if t.organization_id in orgs]


def _warehouse(db: Session, tx_ids: list[int]) -> list[WarehouseReceipt]:
    if not tx_ids:
        return []
    return (db.query(WarehouseReceipt).filter(WarehouseReceipt.transaction_id.in_(tx_ids),
                                              WarehouseReceipt.deleted_at.is_(None))
            .order_by(WarehouseReceipt.id).all())


def card(db: Session, kind: str, item_id: int, rows: list[Transaction]) -> dict:
    """То, что показывает карточка: как у новой покупки."""
    t0 = rows[0]
    total = sum((Decimal(t.amount) for t in rows), Decimal("0"))
    unpaid = sum((Decimal(t.amount) - Decimal(t.amount_paid) for t in rows if t.amount_paid is not None), Decimal("0"))
    wr = _warehouse(db, [t.id for t in rows])
    lines = [{"name": r.product.name, "qty": _fmt_num(r.quantity), "unit": r.product.unit or "",
              "price": fmt_money(float(r.price_per_unit)), "total": fmt_money(float(r.total_cost))} for r in wr]
    receipt = db.get(Receipt, item_id) if kind == "r" else None
    if not lines and receipt is not None:
        lines = [{"name": i.name, "qty": _fmt_num(i.qty), "unit": "", "price": fmt_money(float(i.unit_price or 0)),
                  "total": fmt_money(float(i.total_price or 0))}
                 for i in db.query(ReceiptItem).filter(ReceiptItem.receipt_id == receipt.id).order_by(ReceiptItem.id).all()]
    payer = db.get(User, t0.paid_from_user_id or t0.created_by) if (t0.paid_from_user_id or t0.created_by) else None
    creator = db.get(User, t0.created_by) if t0.created_by else None
    supplier = db.get(Supplier, t0.supplier_id) if t0.supplier_id else None
    if t0.paid_directly:
        payment = "account"
    elif unpaid >= 1 and unpaid >= total - Decimal("0.5"):
        payment = "debt"
    elif unpaid >= 1:
        payment = "part"
    else:
        payment = "cash"
    return {"kind": kind, "id": item_id, "date": t0.date, "supplier": supplier, "total": total, "unpaid": unpaid,
            "paid": total - unpaid, "payment": payment, "payer": payer, "creator": creator,
            "created_at": t0.created_at, "note": t0.description, "lines": lines, "tx_ids": [t.id for t in rows],
            "photo": receipt.file_path if receipt and receipt.file_path != "manual" else None,
            "account_org_id": t0.account_org_id or t0.organization_id, "has_stock": bool(wr)}


def edit_rows(db: Session, kind: str, item_id: int, rows: list[Transaction]) -> list[dict]:
    """Строки для «Купили» из записи старого входа."""
    wr = _warehouse(db, [t.id for t in rows])
    if wr:
        return [_row_dict(r.product, r.quantity, r.price_per_unit) for r in wr]
    if kind == "r":
        out = []
        for i in db.query(ReceiptItem).filter(ReceiptItem.receipt_id == item_id).order_by(ReceiptItem.id).all():
            out.append(_row_dict(i.product if i.product_id else None, i.qty, i.unit_price, name=i.name))
        return out
    return []


def remove(db: Session, rows: list[Transaction], user: User, why: str) -> None:
    """Убрать запись старого входа: мягко, склад и долг откатываются сами."""
    now = datetime.now()
    ids = [t.id for t in rows]
    for t in rows:
        t.deleted_at = now
    for wr in _warehouse(db, ids):
        wr.deleted_at = now
    audit(db, "transaction", ids[0], "delete", user.id, {"tx_ids": ids, "reason": why, "from": "new/record"})
