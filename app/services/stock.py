"""Склад нового входа (блок 4, макет 21.09).

Остаток = приходы с покупок минус листы кухни, пересчёт — редкий контроль по
сигналу. Как в Кассе: строка доверия и пробелы словами. Пробел — только то, что
система видит сама: не внесённый лист кухни (продукты ушли, система не знает),
минус по записям (не внесена покупка), товар без категории (непонятно, считать
ли остаток). Мелочь (зелень, специи, хлеб, моющее…) уходит в расход в день
покупки — её остаток не показываем вовсе.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import (
    AuditLog, Product, ProductAlias, ProductCategory, StockCount, StockCountLine, StockCountPhoto,
    Transaction, User, WarehouseReceipt, WriteOff,
)
from app.services import kitchen
from app.services import stock_count as sc
from app.services.purchases import site_orgs
from app.services.warehouse import get_balance_map

DUST = 0.0005
COUNT_REASON = "пересчёт склада"
MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря"]


def _org_ids(db: Session, site_id: int) -> set[int]:
    return {o.id for o in site_orgs(db, site_id)} | {site_id}


def qty(v) -> str:
    return kitchen.fmt_qty(v)


def _day(d: date) -> str:
    return f"{d.day} {MONTHS_GEN[d.month - 1]}"


def _days_text(days: list[date]) -> str:
    """16–19 сентября, 21 сентября — подряд идущие рабочие дни одним отрезком."""
    if not days:
        return ""
    parts, run = [], [days[0]]
    for d in days[1:]:
        gap = (d - run[-1]).days
        if gap == 1 or (gap == 3 and run[-1].weekday() == 4):     # пятница → понедельник подряд
            run.append(d)
        else:
            parts.append(run)
            run = [d]
    parts.append(run)
    out = []
    for r in parts:
        if len(r) == 1:
            out.append(_day(r[0]))
        elif r[0].month == r[-1].month:
            out.append(f"{r[0].day}–{r[-1].day} {MONTHS_GEN[r[0].month - 1]}")
        else:
            out.append(f"{_day(r[0])} – {_day(r[-1])}")
    return ", ".join(out)


def pack_text(p: Product) -> str:
    if p.pack_name and p.pack_qty:
        return f"{p.pack_name} = {qty(p.pack_qty)} {p.unit or ''}".strip()
    return ""


def _live_products(db: Session) -> list[Product]:
    return db.query(Product).filter(Product.merged_into_id.is_(None), Product.retired_at.is_(None)).all()


def state(db: Session, site_id: int) -> dict:
    """Главный экран: учётные остатки по категориям, доверие, пробелы."""
    org_ids = _org_ids(db, site_id)
    bal = get_balance_map(db, org_ids)
    cats = db.query(ProductCategory).order_by(ProductCategory.sort_order, ProductCategory.name).all()
    by_cat: dict[int | None, list] = {}
    for p in _live_products(db):
        b = float(bal.get(p.id, {}).get("balance", 0) or 0)
        if abs(b) <= DUST or kitchen.is_minor(p):
            continue
        by_cat.setdefault(p.category_id, []).append({"p": p, "balance": b, "pack": pack_text(p)})
    groups = []
    for c in cats:
        if c.id in by_cat and not c.is_minor:
            groups.append({"cat": c, "name": c.name, "rows": sorted(by_cat[c.id], key=lambda r: r["p"].name.lower())})
    no_cat = sorted(by_cat.get(None, []), key=lambda r: r["p"].name.lower())
    if no_cat:
        groups.append({"cat": None, "name": "Без категории", "rows": no_cat})

    gaps = []
    missing = kitchen.missing_days(db, site_id)
    if missing:
        gaps.append({"title": f"Листы кухни не внесены: {_days_text(missing)}",
                     "sub": "пока их нет, остатки больше, чем на полках",
                     "go": f"Внести за {_day(missing[0])}", "url": f"/new/kitchen?date={missing[0].isoformat()}"})
    for g in groups:
        for r in g["rows"]:
            if r["balance"] < -DUST:
                gaps.append({"title": f"{r['p'].name}: по записям минус {qty(-r['balance'])} {r['p'].unit or ''}".rstrip(),
                             "sub": "похоже, не внесена покупка", "go": "Открыть", "url": f"/new/stock/{r['p'].id}"})
    if no_cat:
        names = ", ".join(r["p"].name.lower() for r in no_cat[:3])
        gaps.append({"title": f"{len(no_cat)} {_plural(len(no_cat), 'товар', 'товара', 'товаров')} без категории: {names}{'…' if len(no_cat) > 3 else ''}",
                     "sub": "непонятно, считать ли по ним остаток", "go": "Разобрать", "url": f"/new/stock/{no_cat[0]['p'].id}"})
    last = sc.last_applied(db, site_id)
    return {"groups": groups, "gaps": gaps, "ok": not gaps, "missing": missing, "last_count": last,
            "minor_names": [c.name for c in cats if c.is_minor],
            "count": sum(len(g["rows"]) for g in groups)}


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def has_moves(db: Session, product_id: int) -> bool:
    return bool(db.query(WarehouseReceipt.id).filter(WarehouseReceipt.product_id == product_id,
                                                     WarehouseReceipt.deleted_at.is_(None)).first()
                or db.query(WriteOff.id).filter(WriteOff.product_id == product_id, WriteOff.deleted_at.is_(None)).first())


def product_card(db: Session, site_id: int, p: Product) -> dict:
    org_ids = _org_ids(db, site_id)
    b = float(get_balance_map(db, org_ids).get(p.id, {}).get("balance", 0) or 0)
    moves = []
    recv = (db.query(WarehouseReceipt, Transaction)
            .outerjoin(Transaction, Transaction.id == WarehouseReceipt.transaction_id)
            .filter(WarehouseReceipt.product_id == p.id, WarehouseReceipt.organization_id.in_(org_ids),
                    WarehouseReceipt.deleted_at.is_(None))
            .order_by(WarehouseReceipt.date.desc(), WarehouseReceipt.id.desc()).limit(40).all())
    for r, t in recv:
        counted = r.supplier_name == "Пересчёт склада"
        url = None
        if t is not None:
            url = f"/new/buy/{t.purchase_id}" if t.purchase_id else f"/new/record/t/{t.id}"
        sub = "" if counted else f"{qty(r.quantity)} {p.unit or ''} по {qty(r.price_per_unit)}".strip()
        moves.append({"date": r.date, "id": r.created_at, "title": "Пересчёт, на полке больше" if counted else f"Привоз{', ' + r.supplier_name if r.supplier_name else ''}",
                      "sub": sub, "amount": float(r.quantity), "url": url})
    woffs = (db.query(WriteOff).filter(WriteOff.product_id == p.id, WriteOff.organization_id.in_(org_ids),
                                       WriteOff.deleted_at.is_(None))
             .order_by(WriteOff.date.desc(), WriteOff.id.desc()).limit(40).all())
    for w in woffs:
        if w.reason == COUNT_REASON:
            title, url = "Пересчёт, на полке меньше", None
        elif w.sheet_id or w.reason == kitchen.SHEET_REASON:
            title, url = "Лист кухни", f"/new/kitchen?date={w.date.isoformat()}"
        else:
            title, url = "Списание" + (f": {w.reason}" if w.reason and w.reason != "питание детей" else ", питание детей"), None
        moves.append({"date": w.date, "id": w.created_at, "title": title, "sub": "", "amount": -float(w.quantity), "url": url})
    moves.sort(key=lambda m: (m["date"], m["id"] or datetime.min), reverse=True)
    minor = kitchen.is_minor(p)
    missing = kitchen.missing_days(db, site_id)
    aliases = [a.raw_text for a in db.query(ProductAlias).filter(ProductAlias.product_id == p.id).order_by(ProductAlias.id).all()]
    return {"balance": b, "minor": minor, "moves": moves[:40], "missing": missing, "missing_text": _days_text(missing),
            "aliases": aliases, "pack": pack_text(p), "locked": has_moves(db, p.id),
            "cats": db.query(ProductCategory).order_by(ProductCategory.sort_order, ProductCategory.name).all()}


def edit_product(db: Session, *, user: User, p: Product, category_id: int | None, unit: str, pack_name: str,
                 pack_qty: str, aliases: str) -> None:
    """Сначала всё проверяем, потом меняем — ошибка не оставляет полправки."""
    if category_id is not None and not db.get(ProductCategory, category_id):
        raise ValueError("Нет такой категории")
    unit = unit.strip()
    if unit and unit != (p.unit or "") and has_moves(db, p.id):
        raise ValueError("Единицу не меняют: по ней уже есть привозы и списания")
    pack_name, pack_qty = pack_name.strip(), pack_qty.strip().replace(",", ".")
    n = None
    if pack_name or pack_qty:
        try:
            n = Decimal(pack_qty)
        except Exception:
            n = None
        if not pack_name or n is None or n <= 0:
            raise ValueError("Фасовка: как называется и сколько в одной, числом — например «лоток» и 30")
    old = {"category_id": p.category_id, "unit": p.unit, "pack_name": p.pack_name,
           "pack_qty": float(p.pack_qty) if p.pack_qty is not None else None}
    p.category_id = category_id
    if unit:
        p.unit = unit
    p.pack_name, p.pack_qty = (pack_name, n) if n is not None else (None, None)
    from app.services.products import ensure_alias
    for raw in [a.strip() for a in aliases.replace(";", ",").split(",")]:
        if raw and raw.lower() != p.name.lower():
            ensure_alias(db, raw, p.id)
    new = {"category_id": p.category_id, "unit": p.unit, "pack_name": p.pack_name,
           "pack_qty": float(p.pack_qty) if p.pack_qty is not None else None}
    if new != old:
        db.add(AuditLog(entity_type="product", entity_id=p.id, action="update", user_id=user.id,
                        old_data=old, new_data=new))


def count_rows(db: Session, site_id: int, category_id: int | None) -> dict:
    """Строки пересчёта по одной категории: учётные товары с остатком или привозом за 60 дней."""
    org_ids = _org_ids(db, site_id)
    ws = {w["product_id"]: w for w in sc.working_set(db, org_ids)}
    cats = [c for c in db.query(ProductCategory).order_by(ProductCategory.sort_order, ProductCategory.name).all()
            if not c.is_minor]
    prods = [p for p in _live_products(db) if p.id in ws and not kitchen.is_minor(p)]
    used = {p.category_id for p in prods}
    cats = [c for c in cats if c.id in used]
    if category_id is None and cats:
        category_id = cats[0].id
    rows = [{"p": p, "balance": float(ws[p.id]["balance"])} for p in prods if p.category_id == category_id]
    rows.sort(key=lambda r: r["p"].name.lower())
    return {"cats": cats, "current": category_id, "rows": rows, "has_no_cat": None in used}


def quick_count(db: Session, *, user: User, site_id: int, items: list[tuple[int, Decimal]],
                photo_path: str | None = None, d: date | None = None) -> dict:
    """Пересчёт только того, что посчитали: строка есть — остаток станет как на полке.
    Пишется той же сессией StockCount (история пересчётов одна), сразу применённой."""
    if not items:
        raise ValueError("Не посчитано ни одной строки: впишите, сколько на полке")
    if any(actual < 0 for _, actual in items):
        raise ValueError("На полке не бывает меньше нуля")
    if sc.get_active(db, site_id):
        raise ValueError("Уже идёт пересчёт в старом входе — завершите или отмените его там")
    org_ids = _org_ids(db, site_id)
    bal = get_balance_map(db, org_ids)
    count = StockCount(organization_id=site_id, count_date=d or date.today(), status="active", started_by=user.id)
    db.add(count)
    db.flush()
    for pid, actual in items:
        exp = Decimal(str(bal.get(pid, {}).get("balance", 0) or 0))
        db.add(StockCountLine(count_id=count.id, product_id=pid, mode=sc.MODE_NUMBER, actual_qty=actual,
                              expected_qty=exp, marked_by=user.id))
    if photo_path:
        db.add(StockCountPhoto(count_id=count.id, file_path=photo_path, uploaded_by=user.id))
    db.flush()
    result = sc.apply(db, count, org_ids, user.id)
    db.add(AuditLog(entity_type="stock_count", entity_id=count.id, action="insert", user_id=user.id,
                    new_data={"from": "new/stock", "lines": len(items), "changed": len(result["changed"])}))
    return result
