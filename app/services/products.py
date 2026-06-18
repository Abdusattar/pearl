from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Product, ProductAlias


def _key(raw: str) -> str:
    return raw.strip().lower()


def match_product(db: Session, raw: str) -> Product | None:
    alias = db.query(ProductAlias).filter(
        func.lower(ProductAlias.raw_text) == _key(raw)
    ).first()
    return alias.product if alias else None


def get_or_create_product(db: Session, name: str) -> Product:
    name = name.strip()
    product = db.query(Product).filter(
        func.lower(Product.name) == name.lower()
    ).first()
    if not product:
        product = Product(name=name)
        db.add(product)
        db.flush()
    return product


def ensure_alias(db: Session, raw: str, product_id: int) -> None:
    key = _key(raw)
    exists = db.query(ProductAlias).filter(
        func.lower(ProductAlias.raw_text) == key
    ).first()
    if not exists:
        db.add(ProductAlias(raw_text=raw.strip(), product_id=product_id))
