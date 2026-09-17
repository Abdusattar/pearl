"""Расход без чека (макет 2г, утверждён 15.09, строка «Что это» — 17.09).

Свет, вода, охрана, доставка, ремонт одной суммой. Пишется той же шапкой
`purchases`, что «Купили», только без позиций и без склада: лента, карточка,
«Оплатить» и «Убрать» у них общие. Категорию расхода задаёт «Что это» —
иначе Обзор не отличит свет от ремонта.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import CashFunding, ExpenseCategory, Purchase, Supplier, Transaction, User
from app.services.price_check import fmt_money
from app.services.purchases import audit

# ключ → (подпись, имя категории расхода; None — без категории)
KINDS = {
    "delivery": ("Доставка, такси", "Сервисные расходы"),
    "light": ("Свет", "Электричество"),
    "water": ("Вода", "Вода"),
    "heat": ("Отопление", "Отопление"),
    "net": ("Интернет, связь", "Интернет"),
    "guard": ("Охрана", "Охрана"),
    "repair": ("Ремонт", "Текущий ремонт имущества"),
    "other": ("Другое", None),
}
FOR_ORG_REQUIRED = {"repair"}          # стройка на объект, не в общее (решение 14.09)
PAYMENTS = ("cash", "account", "debt", "founder")
RECENT_SUPPLIERS = 6


def category_id(db: Session, kind: str) -> int | None:
    name = KINDS[kind][1]
    if name is None:
        return None
    row = db.query(ExpenseCategory.id).filter(ExpenseCategory.name == name).first()
    return row[0] if row else None


def kind_of(db: Session, purchase: Purchase) -> str:
    """«Поправить»: какое «Что это» было у расхода — по категории его проводки."""
    tx = next((t for t in purchase.transactions if t.deleted_at is None), None)
    cat = db.get(ExpenseCategory, tx.category_id) if tx and tx.category_id else None
    return next((k for k, (_, name) in KINDS.items() if cat and name == cat.name), "other")


def recent_suppliers(db: Session, site_org_id: int) -> list[Supplier]:
    """Чипы «Кому»: у кого уже были расходы без чека."""
    ids = [r[0] for r in db.query(Purchase.supplier_id)
           .filter(Purchase.site_org_id == site_org_id, Purchase.receipt_id.is_(None), Purchase.deleted_at.is_(None))
           .order_by(Purchase.id.desc()).limit(50).all()]
    seen: list[int] = []
    for i in ids:
        if i not in seen:
            seen.append(i)
    return [db.get(Supplier, i) for i in seen[:RECENT_SUPPLIERS]]


def find_repeat(db: Session, site_org_id: int, kind: str, amount: Decimal, d: date) -> dict | None:
    cat = category_id(db, kind)
    q = (db.query(Purchase).join(Transaction, Transaction.purchase_id == Purchase.id)
         .filter(Purchase.site_org_id == site_org_id, Purchase.receipt_id.is_(None), Purchase.deleted_at.is_(None),
                 Purchase.date == d, Purchase.total == amount, Transaction.deleted_at.is_(None)))
    q = q.filter(Transaction.category_id == cat) if cat else q.filter(Transaction.category_id.is_(None))
    p = q.order_by(Purchase.id.desc()).first()
    if p is None:
        return None
    return {"what": f"{KINDS[kind][0].lower()} {fmt_money(float(amount))}", "date": p.date,
            "by": p.creator.name if p.creator else None, "at": p.created_at.date() if p.created_at else None}


def record(db: Session, *, user: User, site_org_id: int, supplier: Supplier, kind: str, amount: Decimal, what: str | None,
           payment: str, payer_id: int | None, account_org_id: int | None, founder_id: int | None,
           for_org_id: int | None, d: date, repeat_confirmed: bool = False) -> Purchase:
    assert kind in KINDS and payment in PAYMENTS
    if amount <= 0:
        raise ValueError("Сумма должна быть больше нуля")
    description = what or KINDS[kind][0]
    paid_from = payer_id if payment == "cash" else (founder_id if payment == "founder" else None)
    purchase = Purchase(
        site_org_id=site_org_id, supplier_id=supplier.id, date=d, total=amount, payment=payment,
        paid_amount=Decimal("0") if payment == "debt" else amount, paid_from_user_id=paid_from,
        account_org_id=account_org_id if payment == "account" else None,
        founder_id=founder_id if payment == "founder" else None, for_org_id=for_org_id,
        note=description, dup_confirmed=repeat_confirmed, created_by=user.id,
    )
    db.add(purchase)
    db.flush()
    tx = Transaction(
        organization_id=site_org_id, type="expense", amount=amount,
        amount_paid=Decimal("0") if payment == "debt" else None, category_id=category_id(db, kind),
        supplier_id=supplier.id, description=description, date=d, created_by=user.id,
        paid_directly=payment == "account", purchase_id=purchase.id, paid_from_user_id=paid_from,
        account_org_id=purchase.account_org_id,
    )
    db.add(tx)
    db.flush()
    if payment == "founder":
        funding = CashFunding(organization_id=site_org_id, source_type="direct_cash", amount=amount, date=d,
                              taken_by=founder_id, accountable_user_id=founder_id, source_founder_id=founder_id,
                              comment=f"Заплатил учредитель: {supplier.name}", created_by=user.id)
        db.add(funding)
        db.flush()
        purchase.funding_id = funding.id
    audit(db, "purchase", purchase.id, "insert", user.id,
          {"site": site_org_id, "supplier": supplier.id, "total": float(amount), "payment": payment,
           "no_receipt": kind, "tx": tx.id})
    return purchase
