from rapidfuzz import process, fuzz
from sqlalchemy import func, desc
from sqlalchemy.orm import Session

from app.models import Dish, MenuEntry

FUZZY_THRESHOLD = 72
FUZZY_AUTO_MATCH = 85


def _key(raw: str) -> str:
    return raw.strip().lower()


def search_dishes(db: Session, raw: str, limit: int = 8) -> list[dict]:
    """Автокомплит для формы меню — точное/префикс/фаззи по названию."""
    key = _key(raw)
    if not key:
        return []

    all_dishes = db.query(Dish).all()
    result = []
    seen = set()

    for d in all_dishes:
        if d.name.lower() == key:
            result.append({"id": d.id, "name": d.name, "score": 100})
            seen.add(d.id)

    for d in all_dishes:
        if d.id not in seen and d.name.lower().startswith(key):
            result.append({"id": d.id, "name": d.name, "score": 95})
            seen.add(d.id)

    for d in all_dishes:
        if d.id not in seen and key in d.name.lower():
            result.append({"id": d.id, "name": d.name, "score": 85})
            seen.add(d.id)

    remaining = {d.id: d.name for d in all_dishes if d.id not in seen}
    if remaining:
        matches = process.extract(raw, remaining, scorer=fuzz.WRatio, limit=limit)
        for name, score, did in matches:
            if score >= FUZZY_THRESHOLD:
                result.append({"id": did, "name": name, "score": round(score, 1)})

    return result[:limit]


def get_or_create_dish(db: Session, name: str) -> Dish:
    """Возвращает существующее блюдо или создаёт новое. Перед созданием
    ищет похожее по нечёткому совпадению — защита от опечаток (10.07),
    чтобы "Каша рисовая"/"Каша ристовая" не расплодились в разные блюда
    и не разбили будущую статистику по рецептуре."""
    name = name.strip()
    dish = db.query(Dish).filter(func.lower(Dish.name) == name.lower()).first()
    if dish:
        return dish

    candidates = search_dishes(db, name, limit=1)
    if candidates and candidates[0]["score"] >= FUZZY_AUTO_MATCH:
        # WRatio съезжает на partial-ratio при сильном расхождении длин строк
        # в ЛЮБУЮ сторону — короткий текст ложно матчится на длинное существующее
        # блюдо (и наоборот), score >85 даже без смысловой связи (проверено на
        # реальных данных: "Рыбьи биточки на пару" против ". Овсяная каша на
        # молоке со сливочным маслом" дало 85.5). Опечатки похожи по длине в обе
        # стороны, поэтому симметричная проверка их не заденет.
        candidate_len = len(candidates[0]["name"])
        shorter, longer = sorted((candidate_len, len(name)))
        if shorter >= 0.6 * longer:
            matched = db.get(Dish, candidates[0]["id"])
            if matched:
                return matched

    dish = Dish(name=name)
    db.add(dish)
    db.flush()
    return dish


def frequent_dishes(db: Session, meal_type: str, limit: int = 8) -> list[dict]:
    """Блюда, чаще всего встречавшиеся в MenuEntry для этого приёма пищи —
    «быстрый выбор» чипами вместо печатания заново каждую неделю (10.07)."""
    rows = (
        db.query(Dish.id, Dish.name, func.count(MenuEntry.id).label("cnt"))
        .join(MenuEntry, MenuEntry.dish_id == Dish.id)
        .filter(MenuEntry.meal_type == meal_type)
        .group_by(Dish.id, Dish.name)
        .order_by(desc("cnt"))
        .limit(limit)
        .all()
    )
    return [{"id": r.id, "name": r.name} for r in rows]
