"""Обзор для собственников (новый вход, макет блок 6, принят 15.09; переделан 24.09).

За десять секунд понять, всё ли в порядке: статус одной фразой, сигналы над
цифрами, пять цифр, месяц по объектам. Ничего не вводится. Мунара цифры и
колонку школы не видит (решение владельца 15.09).

24.09 (владелец: «оцени информативность, удобство, телефон»): цифры честные —
просрочено отдельно от «ещё не срок», «—» вместо нуля там, где записей нет,
целые сомы, у каждой цифры — когда подтверждена; сигналы только учредительские
(долги, минусы, пробелы денег), операционное («едят не записано») — Махабат в «Сегодня».

Первый шаг: колонки месяца по объекту проводки (organization_id) — общие
расходы площадки пока лежат в колонке садика, правила деления (еда по едокам,
свет по квадратуре) придут с Настройками.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Charge, ExpenseCategory, Organization, Student, Transaction, User
from app.services import cash, children, rules, today
from app.services.ledger import month_bounds
from app.services.purchases import site_orgs
from app.services.warehouse import get_product_balances

FOOD = {"продукты питания", "услуги питания", "бутилированная вода"}
SALARY = {"фот", "соцфонд", "соцфонд и подоходный"}
UTIL = {"коммунальные расходы", "электричество", "вода", "отопление", "интернет", "связь",
        "охрана", "операционные услуги", "реклама"}
# Учредителю — только про деньги и долги; листы, склад, едоки — операционное (24.09)
FOUNDER_SRC = {"bank", "cash", "debt", "receipts"}
WEEKDAY_NOM = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря"]


def _d(d: date | None) -> str:
    if d is None:
        return ""
    today_d = date.today()
    if d == today_d:
        return "сегодня"
    if d == today_d - timedelta(days=1):
        return "вчера"
    return f"{d.day} {MONTHS_GEN[d.month - 1]}"


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
        if it.get("src") not in FOUNDER_SRC:
            continue
        out.append({"kind": "warn" if it["kind"] != "info" else "info", "text": it["title"], "sub": it["sub"], "url": it["url"]})
    # Пробелы Кассы — те же, что видит Махабат (21.09): без «не подтверждал N дней»,
    # пересчёт не ритуал, сигнал только там, где не хватает записи.
    for g in cash.state(db, site_org_id)["gaps"]:
        if g["where"] == "account" and not any(o.name in g["title"] for o in orgs):
            continue
        out.append({"kind": "warn", "text": g["title"], "sub": g["sub"], "url": "/new/cash"})
    return out


def _month_parents(db: Session, org: Organization, first: date, last: date) -> dict:
    """За текущий месяц по объекту: сколько детей, сколько из них уже заплатили,
    начислено и получено. Тестовые PIN не считаем."""
    kids = [s for s in db.query(Student).filter(Student.organization_id == org.id, Student.deleted_at.is_(None),
                                                 Student.status.in_(("active", "frozen"))).all()
            if not children._is_test(s)]
    ids = [s.id for s in kids]
    if not ids:
        return {"count": 0, "paid_kids": 0, "charged": Decimal("0"), "paid": Decimal("0")}
    charged = Decimal(db.query(func.coalesce(func.sum(Charge.amount), 0))
                      .filter(Charge.student_id.in_(ids), Charge.deleted_at.is_(None),
                              Charge.date >= first, Charge.date <= last).scalar())
    rows = (db.query(Transaction.student_id, func.sum(Transaction.amount))
            .filter(Transaction.student_id.in_(ids), Transaction.type == "income", Transaction.deleted_at.is_(None),
                    Transaction.date >= first, Transaction.date <= last).group_by(Transaction.student_id).all())
    paid = sum((Decimal(a) for _, a in rows), Decimal("0"))
    return {"count": len(ids), "paid_kids": len([1 for _, a in rows if a and a > 0]), "charged": charged, "paid": paid}


def figures(db: Session, site_org_id: int, orgs: list[Organization]) -> dict:
    st = cash.state(db, site_org_id)
    org_ids_vis = {o.id for o in orgs}
    acc = [{"org": a["org"], "expected": round(a["expected"]), "since": a["since"], "ok": a["ok"]}
           for a in st["accounts"] if a["org"].id in org_ids_vis]
    org_ids = {o.id for o in site_orgs(db, site_org_id)} | {site_org_id}
    balances = get_product_balances(db, org_ids)
    stock = sum(b["balance_value"] for b in balances
                if b["balance"] > 0 and b["product"].product_category and not b["product"].product_category.is_minor)
    from app.services import stock_count
    last_count = stock_count.last_applied(db, site_org_id)
    cw = rules.count_weekday(db)
    today_d = date.today()
    next_count = today_d + timedelta(days=(cw - today_d.weekday()) % 7 or 7)
    first, last, _ = month_bounds(None)
    last = min(last, today_d)
    parents = []
    for o in orgs:
        d = children.children_list(db, o)
        m = _month_parents(db, o, first, last)
        has_tariff = d["tariff"] is not None
        parents.append({"org": o, "tariff": has_tariff, "overdue": round(d["old_total"]),
                        "count": m["count"], "paid_kids": m["paid_kids"],
                        "waiting": round(max(Decimal("0"), m["charged"] - m["paid"])),
                        "no_payments": has_tariff and m["paid"] == 0})
    debts = today.supplier_debts(db, site_org_id)
    return {
        "accounts": acc, "accounts_total": sum((a["expected"] for a in acc), 0),
        "cash": round(st["cash"]["total"]), "cash_confirmed": st["cash"]["confirmed"],
        "pockets": [{"name": r["user"].name, "balance": round(r["balance"])} for r in st["cash"]["rows"]],
        "stock": round(stock), "stock_since": last_count, "stock_next": next_count,
        "key_count": len(rules.key_products(db)),
        "parents": parents, "overdue_total": sum(p["overdue"] for p in parents if p["tariff"]),
        "suppliers": round(sum((d["debt"] for d in debts), Decimal("0"))),
        "supplier_rows": [{"name": d["name"], "debt": round(d["debt"]), "since": d["since"]} for d in debts[:3]],
        "d": _d, "weekday": WEEKDAY_NOM[next_count.weekday()],
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
                   "other": Decimal("0"), "tariff": True, "has": set()} for o in orgs}
    for org_id, cat_id, total in (db.query(Transaction.organization_id, Transaction.category_id, func.sum(Transaction.amount))
                                  .filter(Transaction.organization_id.in_(list(cols)), Transaction.type == "expense",
                                          Transaction.deleted_at.is_(None), Transaction.date >= first, Transaction.date <= last)
                                  .group_by(Transaction.organization_id, Transaction.category_id).all()):
        k = kinds.get(cat_id, "other")
        cols[org_id][k] += Decimal(total)
        cols[org_id]["has"].add(k)
    for org_id, total in (db.query(Transaction.organization_id, func.sum(Transaction.amount))
                          .filter(Transaction.organization_id.in_(list(cols)), Transaction.type == "income",
                                  Transaction.deleted_at.is_(None), Transaction.date >= first, Transaction.date <= last)
                          .group_by(Transaction.organization_id).all()):
        cols[org_id]["income"] += Decimal(total)
        cols[org_id]["has"].add("income")
    # Передали продукты другому садику (23.09): куплено нами, съедено там — из нашей еды
    # вычитаем по цене закупки, строкой ниже видно, сколько и кому (владелец: без долга).
    from app.services.stock import transfers_value
    sent = transfers_value(db, from_ids=set(cols) | {site_org_id}, since=first, until=last)
    for o in orgs:
        cols[o.id]["sent"] = Decimal("0")
    if sent and site_org_id in cols:
        cols[site_org_id]["food"] -= sent
        cols[site_org_id]["sent"] = sent
    for o in orgs:
        c = cols[o.id]
        c["tariff"] = children.billing.get_tuition_service(db, o.id) is not None
        c["left"] = c["income"] - c["food"] - c["salary"] - c["util"] - c["other"]
        m = _month_parents(db, o, first, last)
        c["charged"] = m["charged"]
        c["income_pct"] = round(float(c["income"] / m["charged"] * 100)) if m["charged"] else None
        c["empty"] = not c["has"]   # ни прихода, ни расхода за месяц — объект ещё не начал (школа)
        for k in ("income", "food", "salary", "util", "other", "left", "sent"):
            c[k] = round(c[k])
    return {"first": first, "last": last, "key": key, "cols": cols,
            "orgs": [o for o in orgs if not cols[o.id]["empty"]],
            "empty_orgs": [o for o in orgs if cols[o.id]["empty"]],
            "prev_month": children.MONTHS_NOM[(first - timedelta(days=1)).month - 1].lower()}


def status_line(sig: list[dict]) -> str:
    n = len([s for s in sig if s["kind"] != "info"])
    if n == 0:
        return "Всё в порядке"
    words = {1: "Одно требует", 2: "Две вещи требуют", 3: "Три вещи требуют", 4: "Четыре вещи требуют"}
    return words.get(n, f"{n} вещей требуют") + " внимания"
