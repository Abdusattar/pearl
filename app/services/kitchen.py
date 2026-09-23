"""Лист кухни: один лист на день, строки — списания (новый вход, 16.09).

План `context/revision/08_kitchen_sheet.md`. Повара пишут на бумаге, что
взяли за день, Махабат переносит раз в несколько дней. Поэтому: день по
умолчанию — первый невнесённый рабочий день; «как вчера» подставляет прошлый
лист; число можно ввести суммой («0,5 + 3,5») и в дробной единице (г вместо
кг) — сервер переводит в единицу карточки; взяли больше, чем числилось, —
списывается что есть, разница записывается расхождением, а не прячется в
минусе остатка (макет 3б).
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import AuditLog, KitchenSheet, Product, StockCount, User, WriteOff
from app.services.purchases import site_orgs
from app.services.warehouse import get_balance_map

SHEET_REASON = "лист кухни"
WORKING_WEEKDAYS = {0, 1, 2, 3, 4}   # пн–пт: кухня в субботу не работает (владелец, 16.09)
LOOKBACK_DAYS = 14                   # невнесённые дни ищем в окне двух недель, не «после последнего листа»:
                                     # Махабат вносит пачками и не по порядку, пропуск раньше внесённого дня
                                     # иначе исчезал бы из списка
SUB_UNITS = {"кг": ("г", 1000), "л": ("мл", 1000)}
EPS = 0.0005


def fmt_qty(v) -> str:
    if v is None:
        return ""
    d = Decimal(str(v)).quantize(Decimal("0.001")).normalize()
    s = format(d, "f")
    return s.replace(".", ",")


def parse_qty(text: str) -> float | None:
    """«3,2» → 3.2; «0,5 + 3,5» → 4.0; пусто → None; мусор → ValueError."""
    text = (text or "").strip()
    if not text:
        return None
    total = 0.0
    for part in re.split(r"\s*\+\s*", text):
        part = part.replace(" ", "").replace(",", ".")
        if not re.fullmatch(r"\d+(\.\d+)?", part):
            raise ValueError(text)
        total += float(part)
    return round(total, 3)


def sub_unit(product: Product) -> tuple[str, int] | None:
    return SUB_UNITS.get(product.unit or "")


def to_card_unit(product: Product, qty: float, chosen_unit: str) -> float:
    """Количество в единице карточки. Разрешены единица карточки и её дробная."""
    unit = product.unit or ""
    if not chosen_unit or chosen_unit == unit:
        return qty
    sub = sub_unit(product)
    if sub and chosen_unit == sub[0]:
        return round(qty / sub[1], 3)
    raise ValueError(f"«{product.name}» считается в {unit}, а в строке выбрано {chosen_unit}")


def stock_map(db: Session, site_org_id: int) -> dict[int, float]:
    org_ids = {o.id for o in site_orgs(db, site_org_id)} | {site_org_id}
    return {pid: v["balance"] for pid, v in get_balance_map(db, org_ids).items()}


def is_minor(product: Product) -> bool:
    return bool(product.product_category and product.product_category.is_minor)


def sheet_for(db: Session, site_org_id: int, d: date) -> KitchenSheet | None:
    return (db.query(KitchenSheet)
            .filter(KitchenSheet.site_org_id == site_org_id, KitchenSheet.date == d,
                    KitchenSheet.deleted_at.is_(None))
            .first())


def last_sheet(db: Session, site_org_id: int, before: date | None = None) -> KitchenSheet | None:
    q = db.query(KitchenSheet).filter(KitchenSheet.site_org_id == site_org_id, KitchenSheet.deleted_at.is_(None))
    if before is not None:
        q = q.filter(KitchenSheet.date < before)
    return q.order_by(KitchenSheet.date.desc()).first()


def missing_days(db: Session, site_org_id: int, until: date | None = None) -> list[date]:
    """Рабочие дни без листа в окне LOOKBACK_DAYS до `until` включительно.
    Пусто, если лист кухни не обязателен (23.09, склад по нормам): тогда ни «Сегодня»,
    ни Склад, ни бот не напоминают о листах — расход даёт недельный пересчёт."""
    from app.services import rules as _rules
    if not _rules.kitchen_sheet_required(db):
        return []
    until = until or date.today()
    start = until - timedelta(days=LOOKBACK_DAYS)
    # пересчёт склада уже привёл остаток к полке — дни до него не пробел
    # (закрытое пересчётом не всплывает, владелец 21.09)
    last_count = (db.query(StockCount.count_date)
                  .filter(StockCount.organization_id == site_org_id, StockCount.status == "applied")
                  .order_by(StockCount.count_date.desc()).first())
    if last_count and last_count[0] >= start:
        start = last_count[0] + timedelta(days=1)
    have = {s.date for s in db.query(KitchenSheet)
            .filter(KitchenSheet.site_org_id == site_org_id, KitchenSheet.deleted_at.is_(None),
                    KitchenSheet.date >= start).all()}
    # день, списанный ещё старым входом (до листа кухни 17.09), внесён — иначе
    # ложный пробел «листы не внесены» за дни, где расход уже записан
    org_ids = {o.id for o in site_orgs(db, site_org_id)} | {site_org_id}
    have |= {d for (d,) in db.query(WriteOff.date)
             .filter(WriteOff.organization_id.in_(org_ids), WriteOff.deleted_at.is_(None),
                     WriteOff.date >= start,
                     or_(WriteOff.reason.is_(None), WriteOff.reason != "пересчёт склада")).distinct().all()}
    from app.services import rules
    working = rules.kitchen_weekdays(db)   # из Настроек (21.09), по умолчанию пн–пт
    days = []
    d = start
    while d <= until:
        if d.weekday() in working and d not in have:
            days.append(d)
        d += timedelta(days=1)
    return days


def default_day(db: Session, site_org_id: int) -> date:
    days = missing_days(db, site_org_id)
    return days[0] if days else date.today()


def next_missing_day(db: Session, site_org_id: int, after: date) -> date | None:
    return next((d for d in missing_days(db, site_org_id) if d > after), None)


def _row(product: Product, qty=None, balance: float | None = None, chosen_unit: str | None = None) -> dict:
    sub = sub_unit(product)
    minor = is_minor(product)
    return {
        "product_id": product.id, "name": product.name, "unit": product.unit or "",
        "sub_unit": sub[0] if sub else None, "sub_factor": sub[1] if sub else None,
        "chosen_unit": chosen_unit if chosen_unit in {product.unit, sub[0] if sub else None} else (product.unit or ""),
        "qty": fmt_qty(qty) if qty is not None else "",
        "minor": minor,
        "balance": None if minor or balance is None else balance,
        "balance_text": "" if minor or balance is None else f"{fmt_qty(balance)} {product.unit or ''}",
        "error": None,
    }


def sheet_rows(db: Session, sheet: KitchenSheet, balances: dict[int, float]) -> list[dict]:
    """Строки внесённого листа для правки: взяли = списано + расхождение."""
    shortfall_by_pid = {s["product_id"]: float(s["taken"]) for s in (sheet.shortfalls or [])}
    lines = (db.query(WriteOff)
             .filter(WriteOff.sheet_id == sheet.id, WriteOff.deleted_at.is_(None))
             .order_by(WriteOff.id).all())
    rows = []
    for w in lines:
        taken = shortfall_by_pid.get(w.product_id, float(w.quantity))
        # остаток «до этого листа»: своё списание возвращается
        bal = balances.get(w.product_id, 0.0) + float(w.quantity)
        rows.append(_row(w.product, taken, bal))
    return rows


def like_last_rows(db: Session, site_org_id: int, before: date, balances: dict[int, float]) -> tuple[date | None, list[dict]]:
    prev = last_sheet(db, site_org_id, before=before)
    if prev is None:
        return None, []
    rows = sheet_rows(db, prev, balances)
    # остаток для нового дня — текущий, без возврата чужих строк
    for r in rows:
        r["balance"] = None if r["minor"] else balances.get(r["product_id"], 0.0)
        r["balance_text"] = "" if r["minor"] else f"{fmt_qty(r['balance'])} {r['unit']}"
    return prev.date, rows


def rows_as_submitted(db: Session, form: dict, balances: dict[int, float], errors: dict | None = None) -> list[dict]:
    errors = errors or {}
    rows = []
    for i, pid in enumerate(form.get("item_product_id", [])):
        pid = (pid or "").strip()
        name = (form["item_name"][i] if i < len(form.get("item_name", [])) else "").strip()
        qty = (form["item_qty"][i] if i < len(form.get("item_qty", [])) else "").strip()
        unit = (form["item_unit"][i] if i < len(form.get("item_unit", [])) else "").strip()
        product = db.get(Product, int(pid)) if pid.isdigit() else None
        if product:
            row = _row(product, None, balances.get(product.id, 0.0), unit)
        else:
            row = {"product_id": None, "name": name, "unit": "", "sub_unit": None, "sub_factor": None,
                   "chosen_unit": "", "qty": "", "minor": False, "balance": None, "balance_text": "", "error": None}
        row["qty"] = qty
        row["error"] = errors.get(i)
        rows.append(row)
    return rows


def resolve_rows(db: Session, form: dict) -> tuple[list[dict], dict[int, str]]:
    """Строки формы → [{product, qty (в единице карточки)}]; ошибки по индексу."""
    items, errors = [], {}
    for i, pid in enumerate(form.get("item_product_id", [])):
        pid = (pid or "").strip()
        name = (form["item_name"][i] if i < len(form.get("item_name", [])) else "").strip()
        qty_text = (form["item_qty"][i] if i < len(form.get("item_qty", [])) else "").strip()
        unit = (form["item_unit"][i] if i < len(form.get("item_unit", [])) else "").strip()
        if not name and not qty_text:
            continue
        product = db.get(Product, int(pid)) if pid.isdigit() else None
        if product is None:
            errors[i] = "Выберите продукт из списка"
            continue
        try:
            qty = parse_qty(qty_text)
        except ValueError:
            errors[i] = "Число или сумма чисел, например 0,5 + 3,5"
            continue
        if qty is None:
            continue
        if qty <= 0:
            errors[i] = "Больше нуля"
            continue
        try:
            qty = to_card_unit(product, qty, unit)
        except ValueError as e:
            errors[i] = str(e)
            continue
        items.append({"product": product, "qty": qty, "index": i})
    return items, errors


def save_sheet(db: Session, *, user: User, site_org_id: int, d: date, items: list[dict],
               children_count: int | None, photo_path: str | None = None, note: str | None = None) -> KitchenSheet:
    """Записывает лист дня. Повторно за тот же день — заменяет строки."""
    now = datetime.now()
    sheet = sheet_for(db, site_org_id, d)
    if sheet is None:
        sheet = KitchenSheet(site_org_id=site_org_id, date=d, created_by=user.id)
        db.add(sheet)
        db.flush()
    else:
        old = db.query(WriteOff).filter(WriteOff.sheet_id == sheet.id, WriteOff.deleted_at.is_(None)).all()
        for w in old:
            w.deleted_at = now
        db.add(AuditLog(entity_type="kitchen_sheet", entity_id=sheet.id, action="replace", user_id=user.id,
                        old_data={"lines": [{"product_id": w.product_id, "qty": float(w.quantity)} for w in old],
                                  "shortfalls": sheet.shortfalls},
                        new_data={"lines": [{"product_id": it["product"].id, "qty": it["qty"]} for it in items]}))
        db.flush()
    sheet.children_count = children_count
    if photo_path:
        sheet.photo_path = photo_path
    if note is not None:
        sheet.note = note or None

    balances = stock_map(db, site_org_id)
    # один товар дважды на листе — считаем вместе, иначе порознь оба пройдут
    wanted: dict[int, float] = {}
    for it in items:
        wanted[it["product"].id] = round(wanted.get(it["product"].id, 0.0) + it["qty"], 3)
    shortfalls = []
    written: dict[int, float] = {}
    for it in items:
        p = it["product"]
        qty = it["qty"]
        if not is_minor(p):
            available = max(balances.get(p.id, 0.0), 0.0) - written.get(p.id, 0.0)
            if qty > available + EPS:
                shortfalls.append({"product_id": p.id, "name": p.name, "unit": p.unit or "",
                                   "taken": round(wanted[p.id], 3), "had": round(max(balances.get(p.id, 0.0), 0.0), 3)})
                qty = round(max(available, 0.0), 3)
        written[p.id] = written.get(p.id, 0.0) + qty
        if qty <= 0:
            continue
        db.add(WriteOff(date=d, product_id=p.id, quantity=qty, organization_id=site_org_id,
                        children_count=children_count, reason=SHEET_REASON, meal_type=None,
                        created_by=user.id, sheet_id=sheet.id))
    # дубли товара в расхождениях схлопываем
    uniq = {}
    for s in shortfalls:
        uniq[s["product_id"]] = s
    sheet.shortfalls = list(uniq.values()) or None
    db.add(AuditLog(entity_type="kitchen_sheet", entity_id=sheet.id, action="save", user_id=user.id,
                    new_data={"date": d.isoformat(), "lines": len(items), "shortfalls": len(uniq)}))
    return sheet
