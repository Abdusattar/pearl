"""Экран «Сегодня» (новый вход, макет блок 1, принят 14.09): не отчёт, а что ждёт
сегодня. Две кнопки, список «Ждёт вас» и три цифры «Сейчас». Всё считается на
лету из существующих сервисов: касса (podotchet), склад (warehouse), долги
(supplier_ledger), листы кухни (kitchen), пересчёт (stock_count), чеки с фото."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import Receipt, Supplier
from app.services import kitchen, podotchet, stock_count
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
    """«Ждёт вас»: сигналы с ссылками. Пусто — на сегодня всё."""
    items = []
    today = date.today()

    for d in kitchen.missing_days(db, site_org_id, until=today)[:3]:
        items.append({"kind": "bad" if d < today else "warn", "url": f"/new/kitchen?date={d.isoformat()}",
                      "title": f"Лист кухни {_day_phrase(d)} не внесён",
                      "sub": f"{_date_short(d)}, склад без расхода за день", "go": "Внести"})
    missing_total = len(kitchen.missing_days(db, site_org_id, until=today))
    if missing_total > 3:
        items[-1]["sub"] += f" · и ещё {missing_total - 3}"

    receipts = unchecked_receipts(db, site_org_id)
    if receipts:
        n = len(receipts)
        word = "чек" if n % 10 == 1 and n % 100 != 11 else ("чека" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "чеков")
        first = receipts[0]
        items.append({"kind": "warn", "url": f"/expenses/{first.id}/confirm",
                      "title": f"{n} {word} с фото не проверен{'' if n == 1 else 'ы'}",
                      "sub": f"сфотографирован{'' if n == 1 else 'ы'} {_date_short(first.created_at.date()) if first.created_at else ''}",
                      "go": "Проверить"})

    for s in supplier_debts(db, site_org_id):
        if s["since"] and (today - s["since"]).days >= DEBT_OLD_DAYS:
            items.append({"kind": "warn", "url": f"/suppliers/{s['id']}",
                          "title": f"{s['name']}: пора платить",
                          "sub": f"{fmt_money(float(s['debt']))} сом, долг с {_date_short(s['since'])}", "go": "Оплатить"})

    active = stock_count.get_active(db, site_org_id)
    if active:
        pr = stock_count.progress(db, active.id)
        days = (today - active.count_date).days
        items.append({"kind": "bad" if days >= STALE_COUNT_DAYS else "warn", "url": "/warehouse/count/",
                      "title": f"Пересчёт склада начат {_date_short(active.count_date)}, не закончен",
                      "sub": f"{pr['marked']} из {pr['total']} отмечено, {days} дн.", "go": "Продолжить"})
    return items


def now_figures(db: Session, site_org_id: int) -> dict:
    """Три цифры «Сейчас»: касса площадки, продукты на складе (основные), долги."""
    cash = podotchet.get_cash_state(db, site_org_id)
    baseline = cash["baseline"]
    org_ids = {o.id for o in site_orgs(db, site_org_id)} | {site_org_id}
    balances = get_product_balances(db, org_ids)
    stock_value = sum(b["balance_value"] for b in balances
                      if b["balance"] > 0 and b["product"].product_category and not b["product"].product_category.is_minor)
    last_count = stock_count.last_applied(db, site_org_id) if hasattr(stock_count, "last_applied") else None
    debts = supplier_debts(db, site_org_id)
    return {
        "cash": float(cash["net"]),
        "cash_when": f"пересчитано {_date_short(baseline['date'])}" if baseline["date"] else "касса ещё не пересчитывалась",
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
