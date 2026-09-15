"""Переход на новый вход: категории с уровнем, слияние и переименование карточек.

Раскладка каталога утверждена владельцем 15.09
(`context/revision/05_catalog_layout.md`, таблица `catalog_layout.csv`).
Здесь только применение: каждая функция умеет работать «вхолостую» и вернуть
описание того, что сделала бы, — владелец смотрит diff по каждой карточке до
записи в прод (правило: боевые данные не правятся без before/after).

Ничего не удаляется. Слитая карточка остаётся указателем `merged_into_id`,
её история (приходы, списания, строки чеков, рецепты, строки пересчёта,
алиасы) переезжает на цель. «Не товар» получает `retired_at`.
"""
from __future__ import annotations

import csv
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import (AccountBalanceSnapshot, CapitalWithdrawal, CashFunding,
                        DishIngredient, Organization, Product, ProductAlias,
                        ProductCategory, ReceiptItem, Reconciliation, StockCount,
                        StockCountLine, WarehouseReceipt, WriteOff)

MINOR_CATEGORIES = ["зелень", "специи", "хлеб и выпечка", "моющее и инвентарь",
                    "канцелярия", "ремонт"]
STOCK_CATEGORIES = ["мясо и рыба", "молочное и яйца", "овощи и фрукты",
                    "крупы и макароны", "бакалея и масла", "сладости и прочее (еда)",
                    "напитки и вода"]
NOT_A_PRODUCT = "не товар"


def ensure_categories(db: Session) -> dict[str, ProductCategory]:
    """Создаёт 13 категорий раскладки, если их ещё нет. Возвращает имя → карточка."""
    existing = {c.name: c for c in db.query(ProductCategory).all()}
    order = 0
    for name in STOCK_CATEGORIES + MINOR_CATEGORIES:
        order += 10
        level = "minor" if name in MINOR_CATEGORIES else "stock"
        cat = existing.get(name)
        if cat is None:
            cat = ProductCategory(name=name, level=level, sort_order=order)
            db.add(cat)
            existing[name] = cat
        else:
            cat.level = level
    db.flush()
    return existing


def _scale(value, factor):
    if value is None or factor == 1:
        return value
    return (Decimal(value) * Decimal(str(factor))).quantize(Decimal("0.001"))


def merge_product(db: Session, source: Product, target: Product, factor: float = 1) -> dict:
    """Переносит всю историю `source` на `target`; `source` остаётся указателем.

    factor — во сколько раз единица источника больше единицы цели (пачка чая
    500 г при цели в граммах → 500): количества умножаются, цена за единицу
    делится. Для мелочи количество остатка не считается, factor=1 достаточно.
    """
    assert source.id != target.id
    moved = {}
    for model in (WarehouseReceipt, WriteOff, StockCountLine):
        rows = db.query(model).filter(model.product_id == source.id).all()
        for row in rows:
            if model is StockCountLine:
                dup = (db.query(StockCountLine)
                       .filter(StockCountLine.count_id == row.count_id,
                               StockCountLine.product_id == target.id).first())
                if dup is not None:
                    # в одном пересчёте у цели уже есть строка: суммируем насчитанное
                    if row.actual_qty is not None:
                        dup.actual_qty = (dup.actual_qty or 0) + _scale(row.actual_qty, factor)
                        dup.mode = dup.mode or row.mode
                    db.delete(row)
                    continue
                row.actual_qty = _scale(row.actual_qty, factor)
                row.expected_qty = _scale(row.expected_qty, factor)
            else:
                row.quantity = _scale(row.quantity, factor)
                if model is WarehouseReceipt and factor != 1:
                    row.price_per_unit = (Decimal(row.price_per_unit) / Decimal(str(factor))).quantize(Decimal("0.01"))
            row.product_id = target.id
        moved[model.__tablename__] = len(rows)
    for model in (ReceiptItem, DishIngredient):
        n = (db.query(model).filter(model.product_id == source.id)
             .update({model.product_id: target.id}, synchronize_session=False))
        moved[model.__tablename__] = n
    n = (db.query(ProductAlias).filter(ProductAlias.product_id == source.id)
         .update({ProductAlias.product_id: target.id}, synchronize_session=False))
    moved["product_aliases"] = n
    # старое имя должно и дальше находить цель
    if not db.query(ProductAlias).filter(func.lower(ProductAlias.raw_text) == source.name.strip().lower()).first():
        db.add(ProductAlias(raw_text=source.name.strip(), product_id=target.id))
    source.merged_into_id = target.id
    source.is_standard = False
    db.flush()
    return moved


def rename_product(db: Session, product: Product, new_name: str) -> None:
    old = product.name
    if old.strip().lower() == new_name.strip().lower():
        product.name = new_name
        return
    clash = db.query(Product).filter(func.lower(Product.name) == new_name.strip().lower()).first()
    if clash is not None and clash.id != product.id:
        raise ValueError(f"имя «{new_name}» уже занято карточкой {clash.id}")
    product.name = new_name.strip()
    if not db.query(ProductAlias).filter(func.lower(ProductAlias.raw_text) == old.strip().lower()).first():
        db.add(ProductAlias(raw_text=old.strip(), product_id=product.id))
    db.flush()


def change_unit(db: Session, product: Product, new_unit: str, factor: float) -> dict:
    """Смена единицы карточки с пересчётом истории (только собственник).
    factor — сколько новых единиц в одной старой (кг → г: 1000)."""
    counts = {}
    for model in (WarehouseReceipt, WriteOff):
        rows = db.query(model).filter(model.product_id == product.id).all()
        for row in rows:
            row.quantity = _scale(row.quantity, factor)
            if model is WarehouseReceipt:
                row.price_per_unit = (Decimal(row.price_per_unit) / Decimal(str(factor))).quantize(Decimal("0.0001"))
        counts[model.__tablename__] = len(rows)
    rows = db.query(StockCountLine).filter(StockCountLine.product_id == product.id).all()
    for row in rows:
        row.actual_qty = _scale(row.actual_qty, factor)
        row.expected_qty = _scale(row.expected_qty, factor)
    counts["stock_count_lines"] = len(rows)
    if product.grams_per_unit is not None:
        product.grams_per_unit = (Decimal(product.grams_per_unit) / Decimal(str(factor))).quantize(Decimal("0.01"))
    product.unit = new_unit
    db.flush()
    return counts


def read_layout(path: str | Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f, delimiter=";"))


def apply_layout(db: Session, rows: list[dict], renames: dict[int, str] | None = None,
                 unit_changes: dict[int, tuple[str, float]] | None = None,
                 merge_factors: dict[int, float] | None = None,
                 dry_run: bool = True) -> list[str]:
    """Применяет раскладку. Возвращает строки diff «было → стало» по карточкам.
    dry_run=True: считает и описывает, но откатывает всё в конце (savepoint)."""
    renames = renames or {}
    unit_changes = unit_changes or {}
    merge_factors = merge_factors or {}
    lines: list[str] = []
    sp = db.begin_nested()
    try:
        cats = ensure_categories(db)
        by_id = {int(r["id"]): r for r in rows}
        products = {p.id: p for p in db.query(Product).filter(Product.id.in_(list(by_id))).all()}
        for pid, r in by_id.items():
            p = products.get(pid)
            if p is None or p.name != r["name"]:
                lines.append(f"! {pid} «{r['name']}»: на этой базе нет такой карточки — пропуск")
                continue
        # 1. категории и вывод «не товар»
        for pid, r in by_id.items():
            p = products.get(pid)
            if p is None or p.name != r["name"]:
                continue
            if r["new_category"] == NOT_A_PRODUCT:
                if p.retired_at is None:
                    p.retired_at = datetime.now()
                    p.is_standard = False
                    lines.append(f"× {p.name}: не товар, убрана из каталога (строки чеков остаются)")
                continue
            cat = cats[r["new_category"]]
            if p.category_id != cat.id:
                lines.append(f"· {p.name}: категория «{p.category or '—'}» → «{cat.name}» ({'мелочь' if cat.is_minor else 'склад'})")
                p.category_id = cat.id
        # 2. смена единицы с коэффициентом — до слияний: коэффициент слияния
        # задан относительно новой единицы цели (пачка чая 500 г при цели в г)
        for pid, (unit, factor) in unit_changes.items():
            p = products.get(pid) or db.get(Product, pid)
            if p is None or p.unit == unit:
                continue
            old_unit = p.unit
            counts = change_unit(db, p, unit, factor)
            what = ", ".join(f"{k} {v}" for k, v in counts.items() if v)
            lines.append(f"⚖ {p.name}: единица {old_unit} → {unit}, история ×{factor}"
                         + (f": {what}" if what else ": истории нет"))
        # 3. слияния (после категорий и единиц: цель уже разложена и в нужной единице)
        for pid, r in by_id.items():
            p = products.get(pid)
            if p is None or p.name != r["name"] or not r["merge_into"]:
                continue
            target = products.get(int(r["merge_into"])) or db.get(Product, int(r["merge_into"]))
            if target is None:
                lines.append(f"! {p.name}: цель слияния {r['merge_into']} не найдена — пропуск")
                continue
            if p.merged_into_id == target.id:
                continue
            factor = merge_factors.get(pid, 1)
            moved = merge_product(db, p, target, factor)
            what = ", ".join(f"{k} {v}" for k, v in moved.items() if v)
            lines.append(f"→ {p.name} ({p.unit}) слита в «{target.name}» ({target.unit})"
                         + (f", ×{factor}" if factor != 1 else "") + (f": {what}" if what else ": истории нет"))
        # 4. переименования
        for pid, new_name in renames.items():
            p = products.get(pid) or db.get(Product, pid)
            if p is None:
                lines.append(f"! {pid}: нет карточки для переименования в «{new_name}»")
                continue
            if p.name != new_name:
                lines.append(f"≡ «{p.name}» → «{new_name}»")
                rename_product(db, p, new_name)
        db.flush()
        if dry_run:
            sp.rollback()
        else:
            sp.commit()
    except Exception:
        if sp.is_active:
            sp.rollback()
        raise
    return lines


# --- Слияние площадки: склад и касса Школы → Садик Сокулук (схема 06, 15.09) ---

def _source_stock(db: Session, org_id: int) -> dict[int, Decimal]:
    """Остаток по товарам объекта: приходы минус списания, без удалённых."""
    recv = dict(db.query(WarehouseReceipt.product_id, func.sum(WarehouseReceipt.quantity))
                .filter(WarehouseReceipt.organization_id == org_id, WarehouseReceipt.deleted_at.is_(None))
                .group_by(WarehouseReceipt.product_id).all())
    woff = dict(db.query(WriteOff.product_id, func.sum(WriteOff.quantity))
                .filter(WriteOff.organization_id == org_id, WriteOff.deleted_at.is_(None))
                .group_by(WriteOff.product_id).all())
    out = {}
    for pid in set(recv) | set(woff):
        qty = Decimal(recv.get(pid) or 0) - Decimal(woff.get(pid) or 0)
        if qty != 0:
            out[pid] = qty
    return out


def merge_site(db: Session, source_id: int, target_id: int, cutoff, reason: str,
               user_id: int | None, dry_run: bool = True) -> list[str]:
    """Склад и касса `source` переезжают к площадке `target`.

    Склад: приходы, списания и пересчёты источника переводятся на площадку;
    на остаток источника по каждому товару добавляется списание датой
    `cutoff` (день пересчёта, который считал общую кухню целиком), так что
    остатки площадки не меняются. Касса: пополнения, изъятия, сверки и точки
    счёта переводятся; пополнение «одолжено у площадки» перестаёт быть
    займом — внутри одной кассы это карман. Транзакции не трогаются: метка
    объекта на расходе остаётся. Брошенный пересчёт площадки без единой
    отметки отменяется. Повторный запуск ничего не меняет.
    """
    lines: list[str] = []
    sp = db.begin_nested()
    try:
        source = db.get(Organization, source_id)
        target = db.get(Organization, target_id)
        if source is None or target is None:
            raise ValueError(f"нет организации {source_id} или {target_id}")
        if source.site_id != target.id:
            lines.append(f"· {source.name}: площадка → «{target.name}»")
            source.site_id = target.id

        stock = _source_stock(db, source_id)
        for pid, qty in sorted(stock.items()):
            p = db.get(Product, pid)
            db.add(WriteOff(date=cutoff, product_id=pid, quantity=qty, organization_id=target_id,
                            reason=reason, created_by=user_id))
            lines.append(f"− {p.name}: остаток {source.name} {qty.normalize():f} {p.unit} списан датой {cutoff:%d.%m} «{reason}»")
        for model, label in ((WarehouseReceipt, "приходов"), (WriteOff, "списаний"), (StockCount, "пересчётов")):
            n = (db.query(model).filter(model.organization_id == source_id)
                 .update({model.organization_id: target_id}, synchronize_session=False))
            if n:
                lines.append(f"→ {label} {source.name} → {target.name}: {n}")

        for f in (db.query(CashFunding).filter(CashFunding.organization_id == source_id)
                  .order_by(CashFunding.date, CashFunding.id).all()):
            what = f"₸ пополнение {f.date:%d.%m} {f.amount:g} ({f.source_type}, подотчётный {f.accountable_user_id}) → {target.name}"
            f.organization_id = target_id
            if f.source_organization_id == target_id:
                f.source_organization_id = None
                what += ", больше не заём"
            lines.append(what)
        for model, label in ((CapitalWithdrawal, "изъятий"), (Reconciliation, "сверок"),
                             (AccountBalanceSnapshot, "точек счёта")):
            n = (db.query(model).filter(model.organization_id == source_id)
                 .update({model.organization_id: target_id}, synchronize_session=False))
            if n:
                lines.append(f"→ {label} {source.name} → {target.name}: {n}")

        for c in db.query(StockCount).filter(StockCount.organization_id == target_id,
                                             StockCount.status == "active").all():
            marked = (db.query(StockCountLine)
                      .filter(StockCountLine.count_id == c.id, StockCountLine.actual_qty.isnot(None)).count())
            if marked:
                lines.append(f"! пересчёт {c.id} от {c.count_date:%d.%m} активен, отметок {marked} — не трогаю")
                continue
            c.status = "cancelled"
            c.cancelled_by = user_id
            c.cancelled_at = datetime.now()
            c.cancel_reason = "слияние складов Сокулука, ничего не отмечено"
            lines.append(f"× пересчёт {c.id} от {c.count_date:%d.%m} отменён: ничего не отмечено")
        db.flush()
        if dry_run:
            sp.rollback()
        else:
            sp.commit()
    except Exception:
        if sp.is_active:
            sp.rollback()
        raise
    return lines
