from rapidfuzz import process, fuzz
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Product, ProductAlias

FUZZY_THRESHOLD = 72


def _key(raw: str) -> str:
    return raw.strip().lower()


def match_product(db: Session, raw: str) -> Product | None:
    """Точное совпадение по alias."""
    alias = db.query(ProductAlias).filter(
        func.lower(ProductAlias.raw_text) == _key(raw)
    ).first()
    return alias.product if alias else None


def rank_candidates(db: Session, raw: str, limit: int = 5, standard_only: bool = True) -> list[dict]:
    """
    Возвращает до `limit` кандидатов из справочника.
    По умолчанию ищет только среди эталонных (is_standard=True).
    Приоритет: точный alias → prefix → substring → fuzzy WRatio → partial_ratio.
    """
    key = _key(raw)
    if not key:
        return []

    base_q = db.query(Product)
    if standard_only:
        base_q = base_q.filter(Product.is_standard == True)

    alias = db.query(ProductAlias).filter(
        func.lower(ProductAlias.raw_text) == key
    ).first()
    if alias and (not standard_only or alias.product.is_standard):
        exact_id = alias.product_id
        result = [{"id": alias.product_id, "name": alias.product.name, "score": 100}]
    else:
        exact_id = None
        result = []

    all_products = base_q.all()
    remaining = [p for p in all_products if p.id != exact_id]

    prefix_ids = set()
    for p in remaining:
        if p.name.lower().startswith(key):
            result.append({"id": p.id, "name": p.name, "score": 95})
            prefix_ids.add(p.id)

    substring_ids = set()
    for p in remaining:
        if p.id not in prefix_ids and key in p.name.lower():
            result.append({"id": p.id, "name": p.name, "score": 85})
            substring_ids.add(p.id)

    skip_ids = prefix_ids | substring_ids | ({exact_id} if exact_id else set())
    choices = {p.id: p.name for p in remaining if p.id not in skip_ids}
    if choices:
        matches = process.extract(raw, choices, scorer=fuzz.WRatio, limit=limit)
        for name, score, pid in matches:
            if score >= FUZZY_THRESHOLD:
                result.append({"id": pid, "name": name, "score": round(score, 1)})

    found_ids = {c["id"] for c in result}
    leftover = {p.id: p.name for p in remaining if p.id not in found_ids}
    if leftover:
        matches = process.extract(raw, leftover, scorer=fuzz.partial_ratio, limit=limit)
        for name, score, pid in matches:
            if score >= FUZZY_THRESHOLD:
                result.append({"id": pid, "name": name, "score": round(score, 1)})

    seen = set()
    deduped = []
    for c in result:
        if c["id"] not in seen:
            seen.add(c["id"])
            deduped.append(c)

    return deduped[:limit]


def get_or_create_product(db: Session, name: str) -> Product:
    """Возвращает существующий продукт или создаёт временный (is_standard=False)."""
    name = name.strip()
    product = db.query(Product).filter(
        func.lower(Product.name) == name.lower()
    ).first()
    if not product:
        product = Product(name=name, is_standard=False)
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


def maybe_promote(db: Session, product: Product, threshold: int = 3) -> bool:
    """
    Авто-промоут временного продукта в эталонный, если он встретился >= threshold раз.
    Возвращает True если продукт был промоутирован.
    Не вызывается для категорий ремонта/стройки — это контролируется на уровне роутера.
    """
    if product.is_standard:
        return False
    from app.models import ReceiptItem
    count = db.query(ReceiptItem).filter(ReceiptItem.product_id == product.id).count()
    if count >= threshold:
        product.is_standard = True
        return True
    return False
