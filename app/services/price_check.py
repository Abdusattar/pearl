"""Проверка цены на приходе (защита единиц, решение владельца 15.09).

Единицу товара человек на форме не вводит — она из карточки. Но если он
считает в лотках, а карточка в штуках, количество и цена за единицу уедут в
разы: яйцо «по 420» вместо 12, укроп «по 180» вместо 12, масло 300 вместо 120
(всё это реальные строки с прода). Единица сама себя не выдаёт, а цена за
единицу — выдаёт. Поэтому перед записью цена сравнивается с обычной ценой
этого товара и при расхождении больше чем в PRICE_ANOMALY_RATIO раз форма
задаёт вопрос, а не молча пишет.

Обычная цена — медиана цен последних приходов за PRICE_HISTORY_DAYS дней
(медиана, а не среднее: в истории уже лежат и 12, и 420). Порог пока
константа, в новом входе уйдёт в Настройки.
"""
from datetime import date, timedelta
from statistics import median

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import Product, WarehouseReceipt

PRICE_ANOMALY_RATIO = 2.0
PRICE_HISTORY_DAYS = 60
PRICE_HISTORY_LIMIT = 10


def fmt_money(v: float) -> str:
    """12.0 → «12», 12.5 → «12,5», 1234.56 → «1 234,56»."""
    s = f"{v:,.2f}".replace(",", " ").replace(".", ",")
    if s.endswith(",00"):
        s = s[:-3]
    elif s.endswith("0"):
        s = s[:-1]
    return s


def usual_price(db: Session, product_id: int, on_date: date | None = None,
                exclude_tx_ids: list[int] | None = None) -> float | None:
    """Медиана цены за единицу по последним приходам товара. None — истории нет."""
    on_date = on_date or date.today()
    q = (
        db.query(WarehouseReceipt.price_per_unit)
        .filter(
            WarehouseReceipt.product_id == product_id,
            WarehouseReceipt.deleted_at.is_(None),
            WarehouseReceipt.date >= on_date - timedelta(days=PRICE_HISTORY_DAYS),
            WarehouseReceipt.price_per_unit > 0,
        )
    )
    if exclude_tx_ids:
        # NOT IN с NULL даёт NULL и молча выкидывает приходы без проводки —
        # поэтому явно оставляем их.
        q = q.filter(or_(
            WarehouseReceipt.transaction_id.is_(None),
            ~WarehouseReceipt.transaction_id.in_(exclude_tx_ids),
        ))
    rows = q.order_by(WarehouseReceipt.date.desc(), WarehouseReceipt.id.desc()).limit(PRICE_HISTORY_LIMIT).all()
    prices = [float(r[0]) for r in rows]
    return median(prices) if prices else None


def price_anomaly_hint(db: Session, product: Product, unit_price: float,
                       on_date: date | None = None,
                       exclude_tx_ids: list[int] | None = None) -> str | None:
    """Текст вопроса для строки формы или None, если цена в норме."""
    if not unit_price or unit_price <= 0:
        return None
    usual = usual_price(db, product.id, on_date, exclude_tx_ids)
    if usual is None or usual <= 0:
        return None
    ratio = unit_price / usual
    if ratio >= PRICE_ANOMALY_RATIO or ratio <= 1 / PRICE_ANOMALY_RATIO:
        return f"{product.name} по {fmt_money(unit_price)}, обычно {fmt_money(usual)} за {product.unit or 'ед.'}. Так?"
    return None
