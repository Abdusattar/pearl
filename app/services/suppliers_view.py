"""Поставщики в новом входе (блок «Расходы», 21.09): список с долгами,
карточка — долг, привозы и оплаты одной лентой, «Уточнить долг».

Долг считает `supplier_ledger`, как и раньше; здесь только то, что видит
человек. Каждая строка истории открывает ту же карточку, что и лента
Расходов: новая покупка — `/new/buy/{id}`, запись старого входа —
`/new/record/...`.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import Purchase, Reconciliation, ReceiptTransaction, Supplier, SupplierPayment, Transaction, User
from app.services import reconciliation
from app.services.price_check import fmt_money
from app.services.purchases import audit
from app.services.supplier_ledger import debt_reset, get_supplier_balance, get_supplier_balances_bulk

ZERO = Decimal("0")


def listing(db: Session) -> list[dict]:
    """Все поставщики: сначала те, кому должны, потом с недавними привозами."""
    sups = db.query(Supplier).all()
    bal = get_supplier_balances_bulk(db, [s.id for s in sups])
    last = dict(db.query(Transaction.supplier_id, Transaction.date).filter(
        Transaction.supplier_id.isnot(None), Transaction.deleted_at.is_(None), Transaction.type == "expense")
        .order_by(Transaction.supplier_id, Transaction.date.desc()).distinct(Transaction.supplier_id).all())
    rows = [{"s": s, "debt": bal.get(s.id, ZERO), "last": last.get(s.id)} for s in sups]
    rows = [r for r in rows if r["debt"] >= 1 or r["last"] is not None]
    rows.sort(key=lambda r: (-(r["debt"] >= 1), -float(r["debt"]), -(r["last"].toordinal() if r["last"] else 0)))
    return rows


def history(db: Session, supplier_id: int, limit: int = 80) -> list[dict]:
    """Привозы, оплаты и уточнения долга одной лентой, новые сверху."""
    txs = (db.query(Transaction).filter(Transaction.supplier_id == supplier_id, Transaction.type == "expense",
                                        Transaction.deleted_at.is_(None))
           .order_by(Transaction.date.desc(), Transaction.id.desc()).limit(400).all())
    rt = dict(db.query(ReceiptTransaction.transaction_id, ReceiptTransaction.receipt_id)
              .filter(ReceiptTransaction.transaction_id.in_([t.id for t in txs])).all()) if txs else {}
    groups: dict = {}
    for t in txs:
        key = ("p", t.purchase_id) if t.purchase_id else (("r", rt[t.id]) if t.id in rt else ("t", t.id))
        g = groups.setdefault(key, {"date": t.date, "at": t.created_at, "amount": ZERO, "unpaid": ZERO,
                                    "direct": t.paid_directly, "note": t.description})
        g["amount"] += Decimal(t.amount)
        if t.amount_paid is not None:
            g["unpaid"] += Decimal(t.amount) - Decimal(t.amount_paid)
    purchases = {p.id: p for p in db.query(Purchase).filter(
        Purchase.id.in_([k[1] for k in groups if k[0] == "p"])).all()} if groups else {}
    items = []
    for key, g in groups.items():
        if key[0] == "p":
            p = purchases.get(key[1])
            url = f"/new/buy/{key[1]}"
            how = {"debt": "в долг", "cash": "из кассы", "account": "со счёта", "part": "часть в долг",
                   "founder": "заплатил учредитель"}.get(p.payment if p else "", "")
        else:
            url = f"/new/record/{key[0]}/{key[1]}"
            how = "со счёта" if g["direct"] else ("в долг" if g["unpaid"] >= g["amount"] - Decimal("0.5") and g["unpaid"] >= 1
                                                 else ("часть в долг" if g["unpaid"] >= 1 else "из кассы"))
        items.append({"date": g["date"], "at": g["at"], "title": "Привоз", "sub": how, "amount": g["amount"], "url": url,
                      "kind": "in"})
    names = {u.id: u.name for u in db.query(User).all()}
    for p in (db.query(SupplierPayment).filter(SupplierPayment.supplier_id == supplier_id,
                                               SupplierPayment.deleted_at.is_(None)).all()):
        src = "со счёта" if p.paid_directly else (f"из кармана {names.get(p.paid_from_user_id, '')}" if p.paid_from_user_id
                                                  else "")
        items.append({"date": p.date, "at": p.created_at, "title": "Оплата", "sub": ", ".join(x for x in (src, p.comment) if x),
                      "amount": -Decimal(p.amount), "url": None, "kind": "out"})
    for r in (db.query(Reconciliation).filter(Reconciliation.kind == reconciliation.SUPPLIER_DEBT,
                                              Reconciliation.subject_id == supplier_id).all()):
        items.append({"date": r.date, "at": r.created_at, "title": "Долг уточнён" + (" (отменено)" if r.cancelled_at else ""),
                      "sub": f"было по записям {fmt_money(float(r.expected_amount))}, стало {fmt_money(float(r.actual_amount))}"
                             + (f". «{r.reason}»" if r.reason else ""),
                      "amount": None, "url": None, "kind": "fix"})
    items.sort(key=lambda x: (x["date"], x["at"] or datetime.min), reverse=True)
    return items[:limit]


def card(db: Session, supplier: Supplier) -> dict:
    reset = debt_reset(db, supplier.id)
    return {"s": supplier, "debt": get_supplier_balance(db, supplier.id), "history": history(db, supplier.id),
            "reset": reset}


def set_debt(db: Session, *, user: User, site_org_id: int, supplier: Supplier, actual: Decimal, reason: str,
             d: date | None = None) -> Reconciliation:
    """«Уточнить долг»: один раз на поставщика, с причиной (правило 07.09). Ошибся —
    отменяем ту запись вместе с владельцем и уточняем заново."""
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("Напишите, откуда цифра: сверили с поставщиком, долг с прошлого года…")
    if (old := debt_reset(db, supplier.id)) is not None:
        raise ValueError(f"Долг {supplier.name} уже уточняли {old.date.strftime('%d.%m.%Y')}. "
                         "Если та цифра неверна, отменим её вместе с владельцем")
    rec = reconciliation.create(db, organization_id=site_org_id, kind=reconciliation.SUPPLIER_DEBT, actual=actual,
                                user_id=user.id, on_date=d or date.today(), subject_id=supplier.id, reason=reason)
    audit(db, "reconciliation", rec.id, "insert", user.id, {"kind": "supplier_debt", "supplier": supplier.id,
                                                            "expected": float(rec.expected_amount), "actual": float(actual)})
    return rec
