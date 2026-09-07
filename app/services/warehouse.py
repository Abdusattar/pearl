from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Product, WarehouseReceipt, WriteOff

ZERO = Decimal("0")


def get_product_balances(db: Session, org_ids: set) -> list[dict]:
    """Остаток и средняя цена закупки по каждому товару, который хоть раз приходовал
    (org_ids: обычно один объект — склад не общий между объектами, см. models.Organization
    про общую кухню школы/садика Сокулук — приходы всё равно пишутся под конкретный org_id)."""
    recv = (
        db.query(
            WarehouseReceipt.product_id.label("pid"),
            func.sum(WarehouseReceipt.quantity).label("qty"),
            func.sum(WarehouseReceipt.total_cost).label("cost"),
        )
        .filter(
            WarehouseReceipt.organization_id.in_(org_ids),
            WarehouseReceipt.deleted_at.is_(None),
        )
        .group_by(WarehouseReceipt.product_id)
        .subquery()
    )

    woff = (
        db.query(
            WriteOff.product_id.label("pid"),
            func.sum(WriteOff.quantity).label("qty"),
        )
        .filter(
            WriteOff.organization_id.in_(org_ids),
            WriteOff.deleted_at.is_(None),
        )
        .group_by(WriteOff.product_id)
        .subquery()
    )

    rows = (
        db.query(
            Product,
            func.coalesce(recv.c.qty, 0).label("received"),
            func.coalesce(recv.c.cost, 0).label("total_cost"),
            func.coalesce(woff.c.qty, 0).label("written"),
        )
        .outerjoin(recv, Product.id == recv.c.pid)
        .outerjoin(woff, Product.id == woff.c.pid)
        .filter(func.coalesce(recv.c.qty, 0) > 0)
        .order_by(Product.category.nullslast(), Product.name)
        .all()
    )

    result = []
    for product, received, total_cost, written in rows:
        received = float(received)
        written = float(written)
        total_cost = float(total_cost)
        balance = received - written
        avg_price = total_cost / received if received > 0 else 0
        result.append({
            "product": product,
            "received": received,
            "written": written,
            "balance": balance,
            "avg_price": avg_price,
            "balance_value": balance * avg_price,
        })
    return result


def get_inventory_summary(db: Session, org_ids: set) -> dict:
    """Стоимость товарного остатка на складе объекта — по средней цене закупки
    (WAC), не текущей рыночной. Считается на лету, ничего не хранится (тот же
    приём, что и касса/счёт в app/services/podotchet.py). negative_count — сколько
    товаров ушло в минус (авто-списание по рецептуре обогнало приход) — сумма
    balance_value молча зачла бы такой минус, поэтому считаем отдельно, чтобы не
    показывать заниженную цифру склада без объяснения."""
    balances = get_product_balances(db, org_ids)
    total = Decimal(str(sum(b["balance_value"] for b in balances))) if balances else ZERO
    negative = [b for b in balances if b["balance"] < 0]
    return {
        "value": total,
        "product_count": len(balances),
        "negative_count": len(negative),
    }
