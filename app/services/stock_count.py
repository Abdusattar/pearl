"""Пересчёт склада — сессия, а не разовая форма (09.09).

Почему сессия: позиций в обороте около 160, Махабат обходит склад с телефоном
и считает мешки. Разовая форма теряла всё введённое при любом прерывании, и
не давала ответить на прямой вопрос владельца «прошлись ли по каждому» —
совпавшая позиция не оставляла в базе следа.

Порядок работы: `start` заводит сессию и строки на все товары рабочего списка,
`mark` отмечает по одной (каждая отметка сразу в базе), `apply` в самом конце
одним разом создаёт приходы и списания. До `apply` остатки не трогаются вообще.
"""
from datetime import date as date_cls, timedelta
from decimal import Decimal

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.models import (
    Product, StockCount, StockCountLine, WarehouseReceipt, WriteOff,
)

# Товар попадает в пересчёт, если он в обороте: закупали за последние
# RECENT_DAYS дней ЛИБО по нему числится ненулевой остаток. Иначе в список
# лезут все 240+ карточек каталога, включая давно неиспользуемые, и обход
# превращается в пролистывание мусора.
RECENT_DAYS = 60
DUST = Decimal("0.001")

# Продуктовые категории — их выносим вперёд отдельной вкладкой: заказчик
# просил, чтобы еда вытаскивалась в первую очередь.
FOOD_CATEGORIES = {
    "овощи", "зелень", "фрукты", "мясо", "молочные", "крупы",
    "бакалея", "масла", "специи", "напитки", "хлеб", "прочее (еда)",
}
# Порядок секций — как реально идёшь по складу, не по алфавиту.
CATEGORY_ORDER = [
    "овощи", "зелень", "фрукты", "мясо", "молочные", "крупы",
    "бакалея", "масла", "специи", "напитки", "хлеб", "прочее (еда)",
]

MODE_SAME = "same"
MODE_ZERO = "zero"
MODE_NUMBER = "number"

ISSUES = {
    "unit": "Единица неверная",
    "duplicate": "Это то же самое, что другой товар",
    "gone": "Такого товара у нас нет",
    "other": "Другое",
}


def _balance_map(db: Session, org_ids: set[int]) -> dict[int, dict]:
    """Остаток и средняя цена по каждому товару — тот же расчёт, что на складе
    (приход минус списание), но по всем товарам, включая те, у которых прихода
    не было ни разу."""
    recv = (
        db.query(
            WarehouseReceipt.product_id.label("pid"),
            func.sum(WarehouseReceipt.quantity).label("qty"),
            func.sum(WarehouseReceipt.total_cost).label("cost"),
        )
        .filter(WarehouseReceipt.organization_id.in_(org_ids),
                WarehouseReceipt.deleted_at.is_(None))
        .group_by(WarehouseReceipt.product_id)
        .subquery()
    )
    woff = (
        db.query(WriteOff.product_id.label("pid"), func.sum(WriteOff.quantity).label("qty"))
        .filter(WriteOff.organization_id.in_(org_ids), WriteOff.deleted_at.is_(None))
        .group_by(WriteOff.product_id)
        .subquery()
    )
    rows = (
        db.query(
            Product.id,
            func.coalesce(recv.c.qty, 0),
            func.coalesce(recv.c.cost, 0),
            func.coalesce(woff.c.qty, 0),
        )
        .outerjoin(recv, Product.id == recv.c.pid)
        .outerjoin(woff, Product.id == woff.c.pid)
        .all()
    )
    out = {}
    for pid, received, cost, written in rows:
        received, cost, written = Decimal(received), Decimal(cost), Decimal(written)
        out[pid] = {
            "balance": received - written,
            # Цены нет у товаров, которые ни разу не приходовались через
            # систему — их карточки создал импорт тех.карты под ингредиент
            # рецепта. Приход по такому товару запишется без стоимости, и это
            # видно на экране завершения, а не молча.
            "avg_price": (cost / received) if received > 0 else Decimal("0"),
            "has_price": received > 0,
        }
    return out


def working_set(db: Session, org_ids: set[int]) -> list[dict]:
    """Товары, которые реально в обороте — из них и составляется пересчёт."""
    since = date_cls.today() - timedelta(days=RECENT_DAYS)
    recent_pids = {
        pid for (pid,) in db.query(WarehouseReceipt.product_id)
        .filter(WarehouseReceipt.organization_id.in_(org_ids),
                WarehouseReceipt.deleted_at.is_(None),
                WarehouseReceipt.date >= since)
        .distinct()
    }
    balances = _balance_map(db, org_ids)
    last_buy = dict(
        db.query(WarehouseReceipt.product_id, func.max(WarehouseReceipt.date))
        .filter(WarehouseReceipt.organization_id.in_(org_ids),
                WarehouseReceipt.deleted_at.is_(None))
        .group_by(WarehouseReceipt.product_id)
        .all()
    )

    result = []
    for p in db.query(Product).order_by(Product.name).all():
        b = balances.get(p.id, {"balance": Decimal("0"), "avg_price": Decimal("0"), "has_price": False})
        if p.id not in recent_pids and abs(b["balance"]) <= DUST:
            continue
        category = p.category or "не разобрано"
        result.append({
            "product_id": p.id,
            "name": p.name,
            "unit": p.unit or "кг",
            "category": category,
            "tab": "food" if p.category in FOOD_CATEGORIES else
                   ("unsorted" if p.category is None else "house"),
            "balance": b["balance"],
            "avg_price": b["avg_price"],
            "has_price": b["has_price"],
            "last_buy": last_buy.get(p.id),
        })
    return result


def category_sort_key(category: str) -> tuple:
    try:
        return (0, CATEGORY_ORDER.index(category), "")
    except ValueError:
        return (1, 0, category)


def get_active(db: Session, organization_id: int) -> StockCount | None:
    return (
        db.query(StockCount)
        .filter(StockCount.organization_id == organization_id, StockCount.status == "active")
        .first()
    )


def start(db: Session, organization_id: int, org_ids: set[int], user_id: int,
          count_date: date_cls | None = None) -> StockCount:
    """Открыть пересчёт и завести строку на каждый товар рабочего списка.

    Строки создаются все сразу, а не по мере отметки: тогда «прошли 159 из 159»
    — это ответ из базы, и видно, по какому именно списку прошли. Если за время
    обхода из чека появится новый товар, состав текущего пересчёта не поплывёт."""
    existing = get_active(db, organization_id)
    if existing:
        return existing

    count = StockCount(
        organization_id=organization_id,
        count_date=count_date or date_cls.today(),
        status="active",
        started_by=user_id,
    )
    db.add(count)
    db.flush()

    for item in working_set(db, org_ids):
        db.add(StockCountLine(count_id=count.id, product_id=item["product_id"]))
    db.flush()
    return count


def mark(db: Session, line: StockCountLine, mode: str, value: Decimal | None,
         expected: Decimal, user_id: int) -> None:
    """Отметить одну позицию. `expected` — остаток по системе прямо сейчас,
    сохраняется снимком, чтобы на завершении заметить движение по товару,
    прошедшее уже после того, как его посчитали."""
    if mode == MODE_SAME:
        actual = expected
    elif mode == MODE_ZERO:
        actual = Decimal("0")
    else:
        actual = Decimal(str(value))
    line.mode = mode
    line.actual_qty = actual
    line.expected_qty = expected
    line.marked_by = user_id
    line.marked_at = func.now()


def unmark(db: Session, line: StockCountLine) -> None:
    """Снять отметку — позиция снова считается непройденной."""
    line.mode = None
    line.actual_qty = None
    line.expected_qty = None
    line.marked_by = None
    line.marked_at = None


def flag(db: Session, line: StockCountLine, issue: str | None, note: str, user_id: int) -> None:
    """Сигнал «тут что-то не так». Карточку товара не меняет — единицы и дубли
    разбираются отдельно, потому что правка задним числом переписывает смысл
    всей прошлой истории по этому товару."""
    line.issue = issue or None
    line.note = (note or "").strip() or None


def progress(db: Session, count_id: int) -> dict:
    total, marked = (
        db.query(func.count(StockCountLine.id),
                 func.count(StockCountLine.actual_qty))
        .filter(StockCountLine.count_id == count_id)
        .one()
    )
    return {"total": total, "marked": marked, "left": total - marked}


def summary(db: Session, count: StockCount, org_ids: set[int]) -> dict:
    """Что произойдёт после завершения — считается на актуальных остатках,
    а не на снимках: цель пересчёта привести систему к факту на момент
    применения."""
    balances = _balance_map(db, org_ids)
    lines = (
        db.query(StockCountLine, Product)
        .join(Product, Product.id == StockCountLine.product_id)
        .filter(StockCountLine.count_id == count.id)
        .all()
    )

    changed, unchanged, no_price, moved, issues = [], 0, [], [], []
    left = 0
    for line, product in lines:
        if line.issue:
            issues.append({"name": product.name, "issue": ISSUES.get(line.issue, line.issue),
                           "note": line.note})
        if line.actual_qty is None:
            left += 1
            continue
        b = balances.get(product.id, {"balance": Decimal("0"), "avg_price": Decimal("0"), "has_price": False})
        delta = Decimal(line.actual_qty) - b["balance"]
        row = {
            "product_id": product.id, "name": product.name, "unit": product.unit or "кг",
            "current": b["balance"], "actual": Decimal(line.actual_qty), "delta": delta,
            "has_price": b["has_price"],
        }
        if abs(delta) <= DUST:
            unchanged += 1
        else:
            changed.append(row)
            if delta > 0 and not b["has_price"]:
                no_price.append(row)
        # Пока позицию считали, по ней прошло движение — молча это проглатывать
        # нельзя, иначе человек не поймёт, почему цифра разошлась с тем, что он
        # видел на экране в момент отметки.
        if line.expected_qty is not None and abs(Decimal(line.expected_qty) - b["balance"]) > DUST:
            moved.append({**row, "was_at_marking": Decimal(line.expected_qty)})

    return {
        "total": len(lines), "marked": len(lines) - left, "left": left,
        "changed": changed, "unchanged": unchanged,
        "no_price": no_price, "moved": moved, "issues": issues,
    }


def apply(db: Session, count: StockCount, org_ids: set[int], user_id: int) -> dict:
    """Завершить пересчёт: привести остатки к насчитанному.

    Разница оформляется тем же способом, что и раньше делала актуализация —
    приход или списание с пометкой «инвентаризация», чтобы движение осталось
    видимым в истории склада, а не появилось из ниоткуда. Неотмеченные позиции
    не трогаются вовсе."""
    result = summary(db, count, org_ids)
    balances = _balance_map(db, org_ids)

    for row in result["changed"]:
        delta = row["delta"]
        b = balances[row["product_id"]]
        if delta > 0:
            price = b["avg_price"]
            db.add(WarehouseReceipt(
                date=count.count_date, product_id=row["product_id"], quantity=delta,
                price_per_unit=price, total_cost=(delta * price).quantize(Decimal("0.01")),
                organization_id=count.organization_id, supplier_name="Пересчёт склада",
                created_by=user_id,
            ))
        else:
            db.add(WriteOff(
                date=count.count_date, product_id=row["product_id"], quantity=abs(delta),
                organization_id=count.organization_id, reason="пересчёт склада",
                created_by=user_id,
            ))

    count.status = "applied"
    count.applied_by = user_id
    count.applied_at = func.now()
    db.flush()
    return result


def cancel(db: Session, count: StockCount, user_id: int, reason: str) -> None:
    """Отменить пересчёт. Запись остаётся — отменённый пересчёт это тоже факт,
    и он не должен выглядеть так, будто его не было."""
    count.status = "cancelled"
    count.cancelled_by = user_id
    count.cancelled_at = func.now()
    count.cancel_reason = (reason or "").strip() or None
    db.flush()
