"""Лента расходов (новый вход, макет 2а) и оплата поставщику (2в/2д).

Одна покупка = одна строка: проводки старого входа, порезанные по категориям,
собираются обратно по квитанции; покупки нового входа — по шапке `purchases`;
зарплата — одной строкой на день выдачи; оплата долга поставщику — строкой
со знаком минус, в сумму месяца не входит (покупка уже посчитана в день
привоза)."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.models import (ExpenseCategory, Purchase, ReceiptItem, ReceiptTransaction, Supplier,
                        SupplierPayment, Transaction, User, WarehouseReceipt)
from app.services.price_check import fmt_money
from app.services.purchases import audit, site_orgs
from app.services.supplier_ledger import get_supplier_balance, get_transaction_remaining_debt_bulk

FOOD_CATEGORY_NAMES = {"продукты питания", "услуги питания"}
SALARY_CATEGORY_NAMES = {"фот", "соцфонд", "соцфонд и подоходный"}


def month_bounds(month: str | None) -> tuple[date, date, str]:
    today = date.today()
    try:
        y, m = (int(x) for x in (month or "").split("-"))
        first = date(y, m, 1)
    except (ValueError, TypeError):
        first = today.replace(day=1)
    nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return first, nxt - timedelta(days=1), first.strftime("%Y-%m")


def _category_kind(db: Session) -> dict[int, str]:
    kinds = {}
    for c in db.query(ExpenseCategory).all():
        n = c.name.lower()
        kinds[c.id] = "food" if n in FOOD_CATEGORY_NAMES else ("salary" if n in SALARY_CATEGORY_NAMES else "other")
    return kinds


def month_rows(db: Session, site_org_id: int, first: date, last: date) -> tuple[list[dict], dict]:
    """Строки ленты за месяц по дням (новые сверху) + итоги."""
    org_ids = [o.id for o in site_orgs(db, site_org_id)]
    txs = (db.query(Transaction)
           .filter(Transaction.organization_id.in_(org_ids), Transaction.type == "expense",
                   Transaction.deleted_at.is_(None), Transaction.date >= first, Transaction.date <= last)
           .order_by(Transaction.date.desc(), Transaction.id.desc()).all())
    tx_ids = [t.id for t in txs]
    receipt_by_tx = dict(db.query(ReceiptTransaction.transaction_id, ReceiptTransaction.receipt_id)
                         .filter(ReceiptTransaction.transaction_id.in_(tx_ids)).all()) if tx_ids else {}
    suppliers = {s.id: s.name for s in db.query(Supplier).all()}
    kinds = _category_kind(db)
    purchases = {p.id: p for p in db.query(Purchase).filter(Purchase.id.in_(
        [t.purchase_id for t in txs if t.purchase_id])).all()} if any(t.purchase_id for t in txs) else {}

    # Долг строки — по расчётам с поставщиком, а не по тому, как записали при
    # закупе: оплаченное позже («Оплатить») иначе вечно висит «в долг» (17.09).
    debt_sids = sorted({t.supplier_id for t in txs if t.supplier_id and t.amount_paid is not None})
    remaining = get_transaction_remaining_debt_bulk(db, debt_sids) if debt_sids else {}

    groups: dict = {}
    order: list = []
    for t in txs:
        if t.purchase_id:
            key = ("p", t.purchase_id)
        elif t.employee_id or (t.category_id and kinds.get(t.category_id) == "salary"):
            key = ("s", t.date, t.period, bool(t.employee_id))   # налог — своей строкой, не внутри зарплаты
        elif t.id in receipt_by_tx:
            key = ("r", receipt_by_tx[t.id])
        else:
            key = ("t", t.id)
        g = groups.get(key)
        if g is None:
            g = {"key": key, "date": t.date, "amount": Decimal("0"), "paid": Decimal("0"), "tx_ids": [],
                 "supplier": suppliers.get(t.supplier_id) if t.supplier_id else None,
                 "description": t.description, "kinds": set(), "people": 0, "paid_directly": t.paid_directly,
                 "purchase": purchases.get(t.purchase_id) if t.purchase_id else None}
            groups[key] = g
            order.append(key)
        g["amount"] += Decimal(t.amount)
        left = remaining.get(t.supplier_id, {}).get(t.id) if t.amount_paid is not None else None
        g["paid"] += Decimal(t.amount) - (Decimal(left) if left is not None else Decimal("0"))
        g["paid_later"] = g.get("paid_later", False) or (t.amount_paid is not None and left is not None
                                                        and Decimal(t.amount) - Decimal(left) > Decimal(t.amount_paid))
        g["tx_ids"].append(t.id)
        g["kinds"].add(kinds.get(t.category_id, "other"))
        if t.employee_id:
            g["people"] += 1

    # позиции: по приходам склада (и старый, и новый вход пишут их)
    all_tx = [i for g in groups.values() for i in g["tx_ids"]]
    names_by_tx: dict[int, list[str]] = {}
    if all_tx:
        rows = (db.query(WarehouseReceipt)
                .filter(WarehouseReceipt.transaction_id.in_(all_tx), WarehouseReceipt.deleted_at.is_(None))
                .order_by(WarehouseReceipt.id).all())
        for r in rows:
            names_by_tx.setdefault(r.transaction_id, []).append(r.product.name)

    out = []
    total = Decimal("0")
    by_kind = {"food": Decimal("0"), "salary": Decimal("0"), "other": Decimal("0")}
    for key in order:
        g = groups[key]
        total += g["amount"]
        kind = "salary" if "salary" in g["kinds"] else ("food" if g["kinds"] == {"food"} else ("other" if "food" not in g["kinds"] else "food"))
        by_kind[kind] += g["amount"]
        names = [n for tid in g["tx_ids"] for n in names_by_tx.get(tid, [])]
        debt = g["amount"] - g["paid"]
        if key[0] == "s":
            if g["people"]:
                title = f"Зарплата за {_month_name(g['date'] if not key[2] else key[2])}"
                sub = f"{g['people']} чел., ведомость"
            else:
                title = g["description"] or "Соцфонд и подоходный"
                sub = "налог" + (f" за {_month_name(key[2])}" if key[2] else "") + (", со счёта" if g["paid_directly"] else "")
            url = f"/new/salary?month={key[2]:%Y-%m}" if key[2] else "/new/salary"
        else:
            title = g["supplier"] or (g["description"] or "Расход")
            if names:
                sub = f"{len(names)} позиц{'ия' if len(names) == 1 else ('ии' if len(names) < 5 else 'ий')}: " + ", ".join(names[:4]) + ("…" if len(names) > 4 else "")
            else:
                sub = g["description"] or "без чека"
            if key[0] == "p":
                url = f"/new/buy/{key[1]}"
            elif key[0] == "r":
                url = f"/new/record/r/{key[1]}"
            else:
                url = f"/new/record/t/{key[1]}"
        if debt >= 1:
            status, status_kind = (f"{fmt_money(float(debt))} в долг" if g["paid"] >= 1 else "в долг"), "debt"
            if g["paid"] >= 1:
                sub += f" · {fmt_money(float(g['paid']))} оплачено"
        elif g.get("paid_later"):
            status, status_kind = "долг оплачен", ""
        elif g["paid_directly"]:
            status, status_kind = "со счёта", ""
        else:
            status, status_kind = "из кассы", ""
        out.append({"date": g["date"], "title": title, "sub": sub, "amount": g["amount"], "status": status,
                    "status_kind": status_kind, "url": url, "payment": False})

    # Оплаты этой площадки и старые без площадки (до 16.09 платёж её не знал).
    pays = (db.query(SupplierPayment)
            .filter(SupplierPayment.deleted_at.is_(None), SupplierPayment.date >= first, SupplierPayment.date <= last,
                    or_(SupplierPayment.organization_id.in_(org_ids + [site_org_id]),
                        SupplierPayment.organization_id.is_(None)))
            .all())
    for p in pays:
        out.append({"date": p.date, "title": f"Оплата: {suppliers.get(p.supplier_id, '')}",
                    "sub": ((p.comment + ", ") if p.comment else "") + ("со счёта" if p.paid_directly else "из кассы")
                           + ". В сумму месяца не входит",
                    "amount": -Decimal(p.amount), "status": "оплата долга", "status_kind": "", "payment": True,
                    "url": f"/new/suppliers/{p.supplier_id}"})
    out.sort(key=lambda r: (r["date"], not r["payment"]), reverse=True)

    days: list[dict] = []
    for r in out:
        if not days or days[-1]["date"] != r["date"]:
            days.append({"date": r["date"], "rows": []})
        days[-1]["rows"].append(r)
    return days, {"total": total, "food": by_kind["food"], "salary": by_kind["salary"], "other": by_kind["other"]}


MONTHS_GEN = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль",
              "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]


def _month_name(d: date) -> str:
    return MONTHS_GEN[d.month - 1]


def pay_supplier(db: Session, *, user: User, site_org_id: int, supplier_id: int, amount: Decimal, d: date,
                 source: str, payer_id: int | None, account_org_id: int | None, comment: str | None) -> SupplierPayment:
    """Оплата долга: уменьшает долг (supplier_ledger) и кассу/счёт (podotchet)."""
    balance = get_supplier_balance(db, supplier_id)
    if amount <= 0:
        raise ValueError("Сумма должна быть больше нуля")
    if amount > balance + Decimal("0.5"):
        raise ValueError(f"Долг сейчас {fmt_money(float(balance))}, больше заплатить нельзя")
    p = SupplierPayment(
        supplier_id=supplier_id, amount=amount, date=d, comment=comment, created_by=user.id,
        organization_id=site_org_id,
        paid_from_user_id=payer_id if source == "cash" else None,
        account_org_id=account_org_id if source == "account" else None,
        paid_directly=source == "account",
    )
    db.add(p)
    db.flush()
    audit(db, "supplier_payment", p.id, "insert", user.id,
          {"supplier_id": supplier_id, "amount": float(amount), "source": source, "site": site_org_id})
    return p
