"""Обзор для собственников (новый вход, макет блок 6, принят 15.09).

За десять секунд понять, всё ли в порядке: статус одной фразой, сигналы над
цифрами, пять цифр, месяц по объектам. Ничего не вводится. Мунара цифры и
колонку школы не видит (решение владельца 15.09).

Первый шаг: колонки месяца по объекту проводки (organization_id) — общие
расходы площадки пока лежат в колонке садика, правила деления (еда по едокам,
свет по квадратуре) придут с Настройками.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import ExpenseCategory, Organization, Purchase, Transaction, User
from app.services import cash, children, today
from app.services.ledger import month_bounds
from app.services.price_check import fmt_money
from app.services.purchases import site_orgs
from app.services.warehouse import get_product_balances

FOOD = {"продукты питания", "услуги питания", "бутилированная вода"}
SALARY = {"фот", "соцфонд"}
UTIL = {"коммунальные расходы", "электричество", "вода", "отопление", "интернет", "связь",
        "охрана", "операционные услуги", "реклама"}
POCKET_STALE_DAYS = 7


def visible_orgs(db: Session, site_org_id: int, user: User) -> list[Organization]:
    """Мунара (manager) видит только садик; остальные — всю площадку."""
    orgs = site_orgs(db, site_org_id)
    if user.role == "manager":
        return [o for o in orgs if o.type == "kindergarten"] or orgs
    if user.role == "director":
        return [o for o in orgs if o.id == user.organization_id] or orgs
    return orgs


def signals(db: Session, site_org_id: int, orgs: list[Organization]) -> list[dict]:
    out = []
    for it in today.todo(db, site_org_id):
        if it["title"].startswith("Лист кухни"):
            continue  # операционное, Махабат видит в «Сегодня»
        out.append({"kind": "warn" if it["kind"] != "info" else "info", "text": it["title"], "sub": it["sub"], "url": it["url"]})
    for p in cash.pockets(db, site_org_id)["rows"]:
        if p["balance"] < -1:
            out.append({"kind": "warn", "text": f"Карман {p['user'].name} в минусе: {fmt_money(float(p['balance']))}",
                        "sub": "тратили из денег, которых в записях нет", "url": "/new/cash"})
        elif not p["start"]["own"] or (p["days"] or 0) > POCKET_STALE_DAYS:
            out.append({"kind": "info", "text": f"{p['user'].name} не подтверждал(а) наличные" + (f" {p['days']} дн." if p["days"] else ""),
                        "sub": f"по записям {fmt_money(float(p['balance']))}", "url": "/new/cash"})
    return out


def figures(db: Session, site_org_id: int, orgs: list[Organization]) -> dict:
    acc = cash.accounts(db, site_org_id)
    acc = [a for a in acc if a["org"].id in {o.id for o in orgs}]
    pk = cash.pockets(db, site_org_id)
    org_ids = {o.id for o in site_orgs(db, site_org_id)} | {site_org_id}
    balances = get_product_balances(db, org_ids)
    stock = sum(b["balance_value"] for b in balances
                if b["balance"] > 0 and b["product"].product_category and not b["product"].product_category.is_minor)
    parents = []
    for o in orgs:
        d = children.children_list(db, o)
        parents.append({"org": o, "debt": d["debt_total"], "tariff": d["tariff"] is not None})
    debts = today.supplier_debts(db, site_org_id)
    return {
        "accounts": acc, "accounts_total": sum((a["expected"] for a in acc), Decimal("0")),
        "cash": pk["total"], "pockets": pk["rows"],
        "stock": round(stock),
        "parents": parents, "parents_total": sum(p["debt"] for p in parents if p["tariff"]),
        "suppliers": sum((d["debt"] for d in debts), Decimal("0")), "supplier_names": ", ".join(d["name"] for d in debts[:3]),
    }


def _kinds(db: Session) -> dict[int, str]:
    kinds = {}
    for c in db.query(ExpenseCategory).all():
        n = c.name.lower()
        kinds[c.id] = "food" if n in FOOD else ("salary" if n in SALARY else ("util" if n in UTIL else "other"))
    return kinds


def month_table(db: Session, site_org_id: int, orgs: list[Organization], month: str | None = None) -> dict:
    first, last, key = month_bounds(month)
    last = min(last, date.today())
    kinds = _kinds(db)
    cols = {o.id: {"income": Decimal("0"), "food": Decimal("0"), "salary": Decimal("0"), "util": Decimal("0"),
                   "other": Decimal("0"), "tariff": True} for o in orgs}
    for org_id, cat_id, total in (db.query(Transaction.organization_id, Transaction.category_id, func.sum(Transaction.amount))
                                  .filter(Transaction.organization_id.in_(list(cols)), Transaction.type == "expense",
                                          Transaction.deleted_at.is_(None), Transaction.date >= first, Transaction.date <= last)
                                  .group_by(Transaction.organization_id, Transaction.category_id).all()):
        cols[org_id][kinds.get(cat_id, "other")] += Decimal(total)
    for org_id, total in (db.query(Transaction.organization_id, func.sum(Transaction.amount))
                          .filter(Transaction.organization_id.in_(list(cols)), Transaction.type == "income",
                                  Transaction.deleted_at.is_(None), Transaction.date >= first, Transaction.date <= last)
                          .group_by(Transaction.organization_id).all()):
        cols[org_id]["income"] += Decimal(total)
    for o in orgs:
        c = cols[o.id]
        c["tariff"] = children.billing.get_tuition_service(db, o.id) is not None
        c["left"] = c["income"] - c["food"] - c["salary"] - c["util"] - c["other"]
    return {"first": first, "last": last, "key": key, "cols": cols}


def status_line(sig: list[dict]) -> str:
    n = len([s for s in sig if s["kind"] != "info"])
    if n == 0:
        return "Всё в порядке"
    words = {1: "Одна вещь требует", 2: "Две вещи требуют", 3: "Три вещи требуют", 4: "Четыре вещи требуют"}
    return words.get(n, f"{n} вещей требуют") + " внимания"
