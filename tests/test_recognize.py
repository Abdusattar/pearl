"""Единый конвейер распознавания (16.09): детерминированная часть без вызова модели."""
import pytest

from app.models import Product
from app.services import recognize as rz
from app.services.transition import ensure_categories


def test_fix_numbers_decimal_shift_prefers_usual_price():
    # картошка: 12.61 × 33 = 4161 → 126,1 (реальный чек 169)
    q, p, t, note = rz.fix_numbers(12.61, 33, 4161, usual=33)
    assert q == pytest.approx(126.1) and p == 33 and t == 4161 and "количество" in note


def test_fix_numbers_swapped_qty_price():
    # филе: «1 × 24 = 365» на самом деле 24 × 365 = 8760 (чек 168) — сумма 365 совпадает с ценой
    q, p, t, note = rz.fix_numbers(1, 24, 365, usual=None)
    assert note and "не сходится" in note        # без обычной цены не свести — но не молчим
    q, p, t, note = rz.fix_numbers(24, 1, 8760, usual=365)
    assert q == 24 and p == pytest.approx(365) and "цена" in note


def test_fix_numbers_missing_one_of_three():
    assert rz.fix_numbers(None, 200, 7800, None)[0] == 39
    assert rz.fix_numbers(26, None, 7800, None)[1] == 300
    assert rz.fix_numbers(26, 300, None, None)[2] == 7800


def test_fix_numbers_consistent_untouched():
    assert rz.fix_numbers(48, 48, 2304, 30) == (48, 48, 2304, None)


def test_unit_conversion_grams_and_pack():
    butter = {"unit": "кг"}
    assert rz.to_card_unit(200, "г", None, butter)[0] == 0.2
    assert rz.to_card_unit(1.5, "кг", 400, butter)[:2] == (1.5, 400)
    pepper = {"unit": "г"}
    assert rz.to_card_unit(0.1, "кг", None, pepper)[0] == 100      # карточка в граммах, на листе кг
    egg = {"unit": "шт", "pack_name": "лоток", "pack_qty": 30}
    q, p, note = rz.to_card_unit(4, "лотка", 420, egg)
    assert q == 120 and p == 14 and "120" in note


@pytest.fixture()
def carrot(db):
    cats = ensure_categories(db)
    p = Product(name="Морковь тест-рз", unit="кг", is_standard=True, category_id=cats["овощи и фрукты"].id)
    db.add(p)
    db.flush()
    return p


def test_post_process_uses_candidate_and_fuzzy_fallback(db, carrot):
    cands = [{"id": carrot.id, "name": carrot.name, "unit": "кг", "usual": 30, "pack_name": None, "pack_qty": None, "minor": False, "balance": None}]
    result = {"lines": [
        {"raw": "морковь", "product_id": carrot.id, "qty": 48, "unit": "кг", "price": 48, "total": 2300},
        {"raw": "морков тест-рз", "product_id": None, "qty": 5, "unit": None, "price": 30, "total": 150},
        {"raw": "Зюзюблик", "product_id": None, "qty": 1, "unit": None, "price": 10, "total": 10},
    ]}
    rows = rz.post_process(db, rz.RECEIPT, result, cands)
    assert rows[0]["product_id"] == carrot.id and rows[0]["total"] == 2304 and rows[0]["notes"] == []
    # нечёткий запасной путь: уверенное совпадение подставляется, сомнительное — вопросом
    assert rows[1]["product_id"] == carrot.id or (rows[1]["question"] or {}).get("kind") == "similar"
    assert rows[2]["product_id"] is None and rows[2]["question"]["kind"] in ("new", "similar")
