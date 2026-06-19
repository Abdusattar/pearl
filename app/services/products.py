from rapidfuzz import process, fuzz
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Product, ProductAlias

FUZZY_THRESHOLD = 72  # минимальный score для показа кандидата


def _key(raw: str) -> str:
    return raw.strip().lower()


def match_product(db: Session, raw: str) -> Product | None:
    """Точное совпадение по alias — для авто-подстановки при подтверждённых alias."""
    alias = db.query(ProductAlias).filter(
        func.lower(ProductAlias.raw_text) == _key(raw)
    ).first()
    return alias.product if alias else None


def rank_candidates(db: Session, raw: str, limit: int = 5) -> list[dict]:
    """
    Возвращает до `limit` кандидатов из справочника отсортированных по схожести.
    Каждый кандидат: {"id": int, "name": str, "score": float}
    Сначала ищет точный alias, затем fuzzy по именам продуктов.
    """
    key = _key(raw)

    # Точный alias — ставим первым с score=100
    alias = db.query(ProductAlias).filter(
        func.lower(ProductAlias.raw_text) == key
    ).first()

    exact_id = alias.product_id if alias else None
    result = []

    if alias:
        result.append({"id": alias.product_id, "name": alias.product.name, "score": 100})

    # Fuzzy по именам продуктов
    all_products = db.query(Product).all()
    choices = {p.id: p.name for p in all_products if p.id != exact_id}

    if choices and raw.strip():
        matches = process.extract(
            raw,
            choices,
            scorer=fuzz.WRatio,
            limit=limit,
        )
        for name, score, pid in matches:
            if score >= FUZZY_THRESHOLD:
                result.append({"id": pid, "name": name, "score": round(score, 1)})

    # Дедупликация по id, сохраняем порядок
    seen = set()
    deduped = []
    for c in result:
        if c["id"] not in seen:
            seen.add(c["id"])
            deduped.append(c)

    return deduped[:limit]


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
