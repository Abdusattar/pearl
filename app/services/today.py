"""Экран «Сегодня» (новый вход, макет блок 1, принят 14.09): не отчёт, а что ждёт
сегодня. Две кнопки, список «Ждёт вас» и три цифры «Сейчас». Всё считается на
лету из существующих сервисов: касса (podotchet), склад (warehouse), долги
(supplier_ledger), листы кухни (kitchen), пересчёт (stock_count), чеки с фото."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import Receipt, Supplier, User
from app.services import cash, kitchen, rules, stock_count
from app.services.price_check import fmt_money
from app.services.purchases import site_orgs, suggest_suppliers
from app.services.supplier_ledger import _bulk_ledger_buckets
from app.services.warehouse import get_product_balances

DEBT_OLD_DAYS = 30
STALE_COUNT_DAYS = 3
MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря"]
WEEKDAYS_ACC = ["понедельник", "вторник", "среду", "четверг", "пятницу", "субботу", "воскресенье"]


def _day_phrase(d: date) -> str:
    """«за пятницу» для этой недели, иначе «за 11 сентября»."""
    today = date.today()
    if 0 < (today - d).days < 7:
        return f"за {WEEKDAYS_ACC[d.weekday()]}"
    if d == today:
        return "за сегодня"
    return f"за {d.day} {MONTHS_GEN[d.month - 1]}"


def _date_short(d: date) -> str:
    return f"{d.day} {MONTHS_GEN[d.month - 1]}"


def supplier_debts(db: Session, site_org_id: int) -> list[dict]:
    """Долги поставщикам с датой самого старого непогашенного долга."""
    suppliers = db.query(Supplier).all()
    buckets = _bulk_ledger_buckets(db, [s.id for s in suppliers])
    names = {s.id: s.name for s in suppliers}
    out = []
    for sid, bs in buckets.items():
        remaining = sum((b["remaining"] for b in bs), Decimal("0"))
        if remaining <= 0:
            continue
        oldest = next((b["date"] for b in bs if b["remaining"] > 0 and b["date"]), None)
        out.append({"id": sid, "name": names.get(sid, "?"), "debt": remaining, "since": oldest})
    out.sort(key=lambda x: -x["debt"])
    return out


def unchecked_receipts(db: Session, site_org_id: int) -> list[Receipt]:
    org_ids = [o.id for o in site_orgs(db, site_org_id)]
    return (db.query(Receipt)
            .filter(Receipt.organization_id.in_(org_ids), Receipt.file_path != "manual",
                    Receipt.ocr_status.in_(("pending", "processed")))
            .order_by(Receipt.created_at.desc()).all())


def todo(db: Session, site_org_id: int) -> list[dict]:
    """«Ждёт вас» (макет 21.09): одна строка на вид дела, важное сверху — листы
    кухни, чеки с фото, пробелы Кассы, пора платить, пробелы Склада. Те же
    пробелы, что на экранах Кассы и Склада, собраны в одном месте. `src` —
    откуда строка (Обзор и бот фильтруют по нему). Пусто — всё внесено."""
    from app.services import stock
    items = []
    today = date.today()

    missing = kitchen.missing_days(db, site_org_id, until=today)
    if missing:
        first = missing[0]
        items.append({"src": "kitchen", "kind": "bad" if first < today else "warn",
                      "url": f"/new/kitchen?date={first.isoformat()}",
                      "title": ("Лист кухни не внесён: " if len(missing) == 1 else "Листы кухни не внесены: ") + stock._days_text(missing),
                      "sub": "пока их нет, склад показывает больше, чем на полках",
                      "go": "Внести за сегодня" if first == today else f"Внести за {_date_short(first)}"})

    # Чеки с фото — одной строкой, список на отдельном экране (туда же придут записи бота).
    receipts = unchecked_receipts(db, site_org_id)
    if receipts:
        last = receipts[0]
        who = db.get(User, last.created_by).name if last.created_by and db.get(User, last.created_by) else None
        n = len(receipts)
        items.append({"src": "receipts", "kind": "warn", "url": "/new/receipts",
                      "title": f"{n} {_plural(n, 'чек', 'чека', 'чеков')} с фото не {'внесён' if n == 1 else 'внесены'}",
                      "sub": "последний " + (f"прислал(а) {who} " if who else "") + (_date_short(last.created_at.date()) if last.created_at else ""),
                      "go": "Разобрать"})

    for g in cash.state(db, site_org_id)["gaps"]:
        items.append({"src": "cash", "kind": "warn", "url": g["url"], "title": g["title"], "sub": g["sub"], "go": g["go"],
                      "where": g["where"]})

    for s in supplier_debts(db, site_org_id):
        if s["since"] and (today - s["since"]).days >= rules.debt_old_days(db):
            items.append({"src": "debt", "kind": "warn", "url": f"/new/pay?supplier={s['id']}",
                          "title": f"{s['name']}: пора платить {fmt_money(float(s['debt']))}",
                          "sub": f"долг с {_date_short(s['since'])}", "go": "Оплатить"})

    for g in stock.state(db, site_org_id)["gaps"]:
        if g["title"].startswith("Лист"):
            continue   # листы кухни уже первой строкой
        items.append({"src": "stock", "kind": "warn", "url": g["url"], "title": g["title"], "sub": g["sub"], "go": g["go"]})

    active = stock_count.get_active(db, site_org_id)
    if active:
        pr = stock_count.progress(db, active.id)
        days = (today - active.count_date).days
        items.append({"src": "count", "kind": "bad" if days >= STALE_COUNT_DAYS else "warn", "url": "/warehouse/count/",
                      "title": f"Пересчёт склада начат {_date_short(active.count_date)}, не закончен",
                      "sub": f"{pr['marked']} из {pr['total']} отмечено, {days} дн.", "go": "Продолжить"})
    return items


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def kitchen_action(db: Session, site_org_id: int) -> dict:
    """Кнопка «Лист кухни»: первый невнесённый день, иначе сегодня."""
    missing = kitchen.missing_days(db, site_org_id)
    d = missing[0] if missing else date.today()
    late = bool(missing) and d < date.today()
    return {"url": f"/new/kitchen?date={d.isoformat()}", "late": late,
            "sub": f"за {_date_short(d)} не внесён" if late else "за сегодня"}


def now_figures(db: Session, site_org_id: int) -> dict:
    """Три цифры «Сейчас»: касса площадки, продукты на складе (основные), долги."""
    # Наличные и счета — из того же источника, что экран Кассы (21.09), с тем же
    # признаком «сходится / не хватает записи»: экраны не расходятся в словах.
    st = cash.state(db, site_org_id)
    org_ids = {o.id for o in site_orgs(db, site_org_id)} | {site_org_id}
    balances = get_product_balances(db, org_ids)
    stock_value = sum(b["balance_value"] for b in balances
                      if b["balance"] > 0 and b["product"].product_category and not b["product"].product_category.is_minor)
    last_count = stock_count.last_applied(db, site_org_id) if hasattr(stock_count, "last_applied") else None
    debts = supplier_debts(db, site_org_id)
    return {
        "cash": float(st["cash"]["total"]), "cash_ok": st["cash"]["ok"],
        "pockets": [{"name": r["user"].name, "amount": float(r["balance"])} for r in st["cash"]["rows"]],
        "accounts": [{"name": a["org"].name, "amount": float(a["expected"]), "ok": a["ok"]} for a in st["accounts"]],
        "stock": round(stock_value),
        "stock_when": (f"пересчёт {_date_short(last_count)}" if last_count else "по приходам и листам кухни"),
        "debt": float(sum((d["debt"] for d in debts), Decimal("0"))),
        "debt_who": ", ".join(d["name"] for d in debts[:3]),
    }


def week_cash_spent(db: Session, site_org_id: int, since: date) -> dict:
    """Расходы из кассы и снятия со счёта с даты — для сигнала «тратили, а снятий нет»."""
    from sqlalchemy import func
    from app.models import CashFunding, Transaction
    spent = db.query(func.coalesce(func.sum(func.coalesce(Transaction.amount_paid, Transaction.amount)), 0)).filter(
        Transaction.organization_id == site_org_id, Transaction.type == "expense", Transaction.paid_directly.is_(False),
        Transaction.deleted_at.is_(None), Transaction.date >= since).scalar()
    withdrawn = db.query(func.coalesce(func.sum(CashFunding.amount), 0)).filter(
        CashFunding.organization_id == site_org_id, CashFunding.deleted_at.is_(None), CashFunding.date >= since).scalar()
    return {"spent": Decimal(spent), "withdrawn": Decimal(withdrawn)}


def buy_subtitle(db: Session, site_org_id: int) -> str:
    names = [s.name for s in suggest_suppliers(db, site_org_id, limit=5)]
    return (", ".join(names) + ". " if names else "") + "Фото чека по желанию"
