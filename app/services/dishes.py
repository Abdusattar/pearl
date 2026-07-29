from rapidfuzz import process, fuzz
from sqlalchemy import func, desc
from sqlalchemy.orm import Session

from app.models import Dish, DishIngredient, DishMergeDismissed, MenuEntry

FUZZY_THRESHOLD = 72
FUZZY_AUTO_MATCH = 85
DUPLICATE_CANDIDATE_THRESHOLD = 60  # ниже, чем FUZZY_THRESHOLD — это ревью человеком,
                                     # не тихое действие, можно закинуть шире сеть


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
        # token_sort_ratio, не WRatio: WRatio на сильном расхождении длин съезжает
        # на partial-ratio и выдаёт один и тот же "случайный" балл (~85.5) вообще
        # без смысловой связи между строками (проверено на реальных данных 23.07 и
        # 27.07 — раз в несколько недель ловим новую пару с этим артефактом).
        # token_sort_ratio честно отражает реальное сходство и не имеет этого сдвига.
        # Сравниваем в нижнем регистре (29.07) — раньше raw/remaining шли как есть,
        # и опечатка с другим регистром ("чй" при реальном "Чай") получала заниженный
        # балл (40 вместо 80) и создавала фантомное блюдо вместо совпадения.
        remaining_lower = {did: nm.lower() for did, nm in remaining.items()}
        matches = process.extract(key, remaining_lower, scorer=fuzz.token_sort_ratio, limit=limit)
        for _, score, did in matches:
            if score >= FUZZY_THRESHOLD:
                result.append({"id": did, "name": remaining[did], "score": round(score, 1)})

    return result[:limit]


def get_or_create_dish(db: Session, name: str, force_new: bool = False) -> Dish:
    """Возвращает существующее блюдо или создаёт новое. Перед созданием
    ищет похожее по нечёткому совпадению — защита от опечаток (10.07),
    чтобы "Каша рисовая"/"Каша ристовая" не расплодились в разные блюда
    и не разбили будущую статистику по рецептуре.

    force_new=True — пользователь на экране Меню уже увидел похожий вариант
    (мягкое подтверждение при вводе, 29.07) и явно сказал "нет, это другое
    блюдо". Уважаем это решение — фаззи-автослияние не включаем даже если
    формально прошёл бы порог, иначе её явный выбор тихо перезапишется."""
    name = name.strip()
    dish = db.query(Dish).filter(func.lower(Dish.name) == name.lower()).first()
    if dish:
        return dish

    if force_new:
        dish = Dish(name=name)
        db.add(dish)
        db.flush()
        return dish

    candidates = search_dishes(db, name, limit=1)
    if candidates and candidates[0]["score"] >= FUZZY_AUTO_MATCH:
        # Доп. страховка поверх скорера: при сильном расхождении длин в ЛЮБУЮ
        # сторону не доверяем авто-матчу, даже если score прошёл порог. Опечатки
        # похожи по длине в обе стороны, поэтому симметричная проверка их не заденет.
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


def find_duplicate_candidates(db: Session, limit: int = 40) -> list[dict]:
    """Кандидаты на слияние для экрана /menu/dishes/duplicates (29.07) — все пары
    блюд каталога с высоким текстовым сходством, кроме уже отклонённых Махабат.
    Только предлагает — ничего не решает и не сливает сама (тот же урок, что и
    с товарами: фаззи-подбор ненадёжен даже на эталонных данных, финальное
    слово всегда за человеком). O(n²) по каталогу — ок при текущих ~100 блюдах,
    пересмотреть, если каталог вырастет на порядок."""
    dishes = db.query(Dish).order_by(Dish.id).all()

    dismissed = {
        (row.dish_id_a, row.dish_id_b)
        for row in db.query(DishMergeDismissed).all()
    }

    ing_counts = dict(
        db.query(DishIngredient.dish_id, func.count(DishIngredient.id))
        .group_by(DishIngredient.dish_id).all()
    )
    usage_counts = dict(
        db.query(MenuEntry.dish_id, func.count(MenuEntry.id))
        .group_by(MenuEntry.dish_id).all()
    )

    def _info(d: Dish) -> dict:
        return {
            "id": d.id, "name": d.name,
            "ingredient_count": ing_counts.get(d.id, 0),
            "times_used": usage_counts.get(d.id, 0),
        }

    candidates = []
    for i, a in enumerate(dishes):
        for b in dishes[i + 1:]:
            pair_key = (a.id, b.id) if a.id < b.id else (b.id, a.id)
            if pair_key in dismissed:
                continue
            score = fuzz.token_sort_ratio(a.name.lower(), b.name.lower())
            if score >= DUPLICATE_CANDIDATE_THRESHOLD:
                candidates.append({
                    "a": _info(a), "b": _info(b), "score": round(score, 1),
                })

    candidates.sort(key=lambda c: -c["score"])
    return candidates[:limit]


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
