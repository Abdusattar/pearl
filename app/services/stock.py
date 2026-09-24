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

from sqlalchemy.orm import Session, joinedload

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
    return (db.query(Product).options(joinedload(Product.product_category))
            .filter(Product.merged_into_id.is_(None), Product.retired_at.is_(None)).all())


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


def counted_view(db: Session, site_id: int, today: date | None = None) -> dict:
    """Склад без живого остатка (владелец 24.09): что на полке, знаем только в день пересчёта.
    По ключевым продуктам — что посчитали на последнем пересчёте и что купили после. Остаток
    между пересчётами не показываем: ежедневного расхода кухни система не знает."""
    from datetime import timedelta
    from sqlalchemy import func
    from app.services import rules
    today = today or date.today()
    keys = rules.key_products(db)
    org_ids = _org_ids(db, site_id)
    last = (db.query(StockCount).filter(StockCount.organization_id.in_(org_ids), StockCount.status == "applied")
            .order_by(StockCount.count_date.desc(), StockCount.id.desc()).first())
    counted: dict[int, Decimal] = {}
    if last is not None:
        # последняя цифра по продукту — с последнего пересчёта, где его считали
        for pid, qty_, _d in (db.query(StockCountLine.product_id, StockCountLine.actual_qty, StockCount.count_date)
                              .join(StockCount, StockCount.id == StockCountLine.count_id)
                              .filter(StockCount.organization_id.in_(org_ids), StockCount.status == "applied",
                                      StockCount.count_date == last.count_date, StockCountLine.actual_qty.isnot(None))
                              .all()):
            counted[pid] = qty_
    since = last.count_date if last is not None else None
    bought = {}
    if since is not None and keys:
        bought = dict(db.query(WarehouseReceipt.product_id, func.sum(WarehouseReceipt.quantity))
                      .filter(WarehouseReceipt.organization_id.in_(org_ids), WarehouseReceipt.product_id.in_(keys),
                              WarehouseReceipt.date > since, WarehouseReceipt.deleted_at.is_(None))
                      .group_by(WarehouseReceipt.product_id).all())
    rows, missing = [], []
    for p in (db.query(Product).filter(Product.id.in_(keys or [0]), Product.retired_at.is_(None),
                                       Product.merged_into_id.is_(None)).all()):
        r = {"p": p, "counted": counted.get(p.id), "bought": bought.get(p.id)}
        (rows if p.id in counted else missing).append(r)
    rows.sort(key=lambda r: r["p"].name.lower())
    missing.sort(key=lambda r: r["p"].name.lower())
    wd = rules.count_weekday(db)
    # ближайший день пересчёта с сегодняшнего, но не раньше чем через 5 дней после прошлого
    # (23.09 посчитали в среду — четверг 24.09 не считаем, следующий 1.10)
    nxt = today + timedelta(days=(wd - today.weekday()) % 7)
    while since is not None and (nxt - since).days < 5:
        nxt += timedelta(days=7)
    all_products = sorted(_live_products(db), key=lambda p: p.name.lower())
    return {"last": since, "next": nxt, "rows": rows, "missing": missing, "all": all_products}


def last_count_diff(db: Session, site_id: int) -> dict | None:
    """Последний пересчёт: где разошлось с записями (24.09: контроль раз в неделю)."""
    from app.models import StockCount, StockCountLine
    c = (db.query(StockCount).filter(StockCount.organization_id == site_id, StockCount.status == "applied")
         .order_by(StockCount.count_date.desc()).first())
    if c is None:
        return None
    bad, ok = [], []
    for ln in db.query(StockCountLine).filter(StockCountLine.count_id == c.id).all():
        if ln.actual_qty is None or ln.expected_qty is None:
            continue
        p = db.get(Product, ln.product_id)
        if p is None:
            continue
        diff = float(ln.actual_qty) - float(ln.expected_qty)
        tol = max(0.05, abs(float(ln.expected_qty)) * 0.02)
        (bad if abs(diff) > tol else ok).append({"p": p, "expected": float(ln.expected_qty),
                                                 "actual": float(ln.actual_qty), "diff": diff})
    bad.sort(key=lambda r: r["diff"])
    return {"date": c.count_date, "bad": bad, "ok": ok, "total": len(bad) + len(ok)}


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
        elif w.to_org_id:
            title, url = f"Передали: {w.to_org.name if w.to_org else 'другой садик'}", None
        elif w.sheet_id or w.reason == kitchen.SHEET_REASON:
            title, url = "Лист кухни", f"/new/kitchen?date={w.date.isoformat()}"
        else:
            title, url = "Списание" + (f": {w.reason}" if w.reason and w.reason != "питание детей" else ", питание детей"), None
        moves.append({"date": w.date, "id": w.created_at, "title": title, "sub": "", "amount": -float(w.quantity), "url": url})
    moves.sort(key=lambda m: (m["date"], m["id"] or datetime.min), reverse=True)
    minor = kitchen.is_minor(p)
    missing = kitchen.missing_days(db, site_id)
    aliases = [a.raw_text for a in db.query(ProductAlias).filter(ProductAlias.product_id == p.id).order_by(ProductAlias.id).all()]
    from sqlalchemy import func
    last = (db.query(StockCountLine.actual_qty, StockCount.count_date)
            .join(StockCount, StockCount.id == StockCountLine.count_id)
            .filter(StockCountLine.product_id == p.id, StockCount.organization_id.in_(org_ids),
                    StockCount.status == "applied", StockCountLine.actual_qty.isnot(None))
            .order_by(StockCount.count_date.desc(), StockCount.id.desc()).first())
    counted, counted_date, bought_since = (last[0], last[1], None) if last else (None, None, None)
    if counted_date is not None:
        bought_since = (db.query(func.sum(WarehouseReceipt.quantity))
                        .filter(WarehouseReceipt.product_id == p.id, WarehouseReceipt.organization_id.in_(org_ids),
                                WarehouseReceipt.date > counted_date, WarehouseReceipt.deleted_at.is_(None))
                        .scalar())
    return {"balance": b, "counted": counted, "counted_date": counted_date, "bought_since": bought_since,
            "minor": minor, "moves": moves[:40], "missing": missing, "missing_text": _days_text(missing),
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


KEY = "key"


DRAFT = "draft"


def draft_count_rows(db: Session, site_id: int, rows: list[dict]) -> dict:
    """Пересчёт по листу из чата (23.09): строки листа, узнанные в каталоге, с учётным
    остатком; мелочь и неузнанное — отдельным списком словами, в остаток не идут."""
    org_ids = _org_ids(db, site_id)
    bal = get_balance_map(db, org_ids)
    out, values, skipped, seen = [], {}, [], {}
    for r in rows:
        pid = r.get("product_id")
        p = db.get(Product, pid) if pid else None
        if p is None or kitchen.is_minor(p):
            skipped.append(r.get("raw") or r.get("name") or "")
            continue
        if pid in seen:
            # тот же товар ниже — это уточнение («масло всего 70 л»): побеждает последняя строка
            seen[pid]["note"] = r.get("note") or f"уточнено: «{r.get('raw')}»"
        else:
            seen[pid] = {"p": p, "balance": float(bal.get(pid, {}).get("balance", 0) or 0), "note": r.get("note")}
            out.append(seen[pid])
        if r.get("qty") is not None:
            values[str(pid)] = kitchen.fmt_qty(r["qty"])
    return {"rows": out, "values": values, "skipped": [s for s in skipped if s]}


def count_rows(db: Session, site_id: int, category_id: int | str | None) -> dict:
    """Строки пересчёта по одной категории: учётные товары с остатком или привозом за 60 дней.
    «Ключевые» (23.09) — первый чип и по умолчанию, если в Настройках они выбраны:
    их считают каждую неделю, из них потом нормы на едока."""
    from app.services import rules
    org_ids = _org_ids(db, site_id)
    ws = {w["product_id"]: w for w in sc.working_set(db, org_ids)}
    cats = [c for c in db.query(ProductCategory).order_by(ProductCategory.sort_order, ProductCategory.name).all()
            if not c.is_minor]
    prods = [p for p in _live_products(db) if p.id in ws and not kitchen.is_minor(p)]
    used = {p.category_id for p in prods}
    cats = [c for c in cats if c.id in used]
    keys = rules.key_products(db)
    if category_id is None:
        category_id = KEY if keys else (cats[0].id if cats else None)
    if category_id == KEY:
        bal = get_balance_map(db, org_ids)
        rows = [{"p": p, "balance": float(bal.get(p.id, {}).get("balance", 0) or 0)}
                for p in _live_products(db) if p.id in set(keys)]
    else:
        rows = [{"p": p, "balance": float(ws[p.id]["balance"])} for p in prods if p.category_id == category_id]
    rows.sort(key=lambda r: r["p"].name.lower())
    return {"cats": cats, "current": category_id, "rows": rows, "has_no_cat": None in used, "n_keys": len(keys)}


TRANSFER_REASON = "передано в другой садик"


def transfer_targets(db: Session, site_id: int) -> list:
    """Куда можно передать: садики и школы вне этой площадки (Кожомкул для Сокулука),
    кроме групп-папок («Садики»), у которых есть дочерние."""
    from app.models import Organization
    mine = _org_ids(db, site_id)
    parents = {pid for (pid,) in db.query(Organization.parent_id).filter(Organization.parent_id.isnot(None)).all()}
    return [o for o in db.query(Organization).filter(Organization.type.in_(("kindergarten", "school"))).order_by(Organization.id).all()
            if o.id not in mine and o.id not in parents and o.site_id not in mine]


def last_price(db: Session, org_ids: set[int], product_id: int) -> Decimal | None:
    """Цена последней закупки (не пересчёта): так владелец велел оценивать передачу."""
    r = (db.query(WarehouseReceipt.price_per_unit)
         .filter(WarehouseReceipt.product_id == product_id, WarehouseReceipt.organization_id.in_(org_ids),
                 WarehouseReceipt.deleted_at.is_(None), WarehouseReceipt.transaction_id.isnot(None),
                 WarehouseReceipt.price_per_unit > 0)
         .order_by(WarehouseReceipt.date.desc(), WarehouseReceipt.id.desc()).first())
    return Decimal(r[0]) if r else None


def transfer_rows(db: Session, site_id: int, include: set[int] | None = None) -> list[dict]:
    """Что можно передать: всё, что есть на складе по записям, и то, что названо в
    черновике (по записям его может не быть, если покупку не внесли)."""
    bal = get_balance_map(db, _org_ids(db, site_id))
    include = include or set()
    rows = [{"p": p, "balance": float(bal.get(p.id, {}).get("balance", 0) or 0)} for p in _live_products(db)]
    return sorted([r for r in rows if r["balance"] > DUST or r["p"].id in include],
                  key=lambda r: (r["p"].id not in include, r["p"].name.lower()))


def transfer_out(db: Session, *, user: User, site_id: int, to_org_id: int, items: list[tuple[int, Decimal]],
                 d: date | None = None, note: str | None = None) -> list[WriteOff]:
    """Передача в другой садик (владелец 23.09): со склада уходит, но это не расход
    кухни — в нормы не идёт; стоимость по цене закупки ложится на получателя, долга
    между точками нет. Когда получатель заведёт склад, эти строки станут его приходом."""
    if to_org_id not in {o.id for o in transfer_targets(db, site_id)}:
        raise ValueError("Выберите, какому садику передали")
    items = [(pid, q) for pid, q in items if q and q > 0]
    if not items:
        raise ValueError("Впишите, сколько чего передали")
    org_ids = _org_ids(db, site_id)
    out = []
    for pid, q in items:
        w = WriteOff(date=d or date.today(), product_id=pid, quantity=q, organization_id=site_id,
                     reason=TRANSFER_REASON, to_org_id=to_org_id, unit_cost=last_price(db, org_ids, pid),
                     created_by=user.id)
        db.add(w)
        out.append(w)
    db.flush()
    db.add(AuditLog(entity_type="stock_transfer", entity_id=out[0].id, action="insert", user_id=user.id,
                    new_data={"to": to_org_id, "lines": [(pid, float(q)) for pid, q in items], "note": note}))
    return out


def transfers_value(db: Session, *, from_ids: set[int] | None = None, to_id: int | None = None,
                    since: date | None = None, until: date | None = None) -> Decimal:
    """Сколько передано в сомах: «Сокулук передал» / «Кожомкул получил» за период."""
    q = db.query(WriteOff).filter(WriteOff.to_org_id.isnot(None), WriteOff.deleted_at.is_(None))
    if from_ids:
        q = q.filter(WriteOff.organization_id.in_(from_ids))
    if to_id:
        q = q.filter(WriteOff.to_org_id == to_id)
    if since:
        q = q.filter(WriteOff.date >= since)
    if until:
        q = q.filter(WriteOff.date <= until)
    return sum((Decimal(w.quantity) * Decimal(w.unit_cost or 0) for w in q.all()), Decimal("0"))


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
    result["count_id"] = count.id
    db.add(AuditLog(entity_type="stock_count", entity_id=count.id, action="insert", user_id=user.id,
                    new_data={"from": "new/stock", "lines": len(items), "changed": len(result["changed"])}))
    return result
