"""
Тесты справочника продуктов и нормализации позиций.
pytest -q tests/test_products.py
"""
from app.models import Product, ProductAlias
from app.services.products import (
    ensure_alias,
    get_or_create_product,
    match_product,
)


def test_match_product_unknown_returns_none(db):
    """Несуществующий алиас → match_product возвращает None."""
    result = match_product(db, "__test_unknown_item__")
    assert result is None


def test_get_or_create_product_creates_new(db):
    """get_or_create_product создаёт продукт если его нет."""
    p = get_or_create_product(db, "__Тест_Продукт__")
    assert p.id is not None
    assert p.name == "__Тест_Продукт__"


def test_get_or_create_product_idempotent(db):
    """Повторный вызов с тем же именем возвращает тот же продукт."""
    p1 = get_or_create_product(db, "__Тест_Идем__")
    p2 = get_or_create_product(db, "__Тест_Идем__")
    assert p1.id == p2.id


def test_ensure_alias_links_raw_to_product(db):
    """ensure_alias создаёт алиас, match_product его находит."""
    product = get_or_create_product(db, "__Тест_Лук__")
    ensure_alias(db, "__test_piyas__", product.id)

    found = match_product(db, "__test_piyas__")
    assert found is not None
    assert found.id == product.id
    assert found.name == "__Тест_Лук__"


def test_ensure_alias_case_insensitive(db):
    """Алиас находится независимо от регистра OCR-текста."""
    product = get_or_create_product(db, "__Тест_Картофель__")
    ensure_alias(db, "__Test_Potato__", product.id)

    assert match_product(db, "__test_potato__") is not None
    assert match_product(db, "__TEST_POTATO__") is not None


def test_ensure_alias_idempotent(db):
    """Двойной вызов ensure_alias не создаёт дубль."""
    product = get_or_create_product(db, "__Тест_Укроп__")
    ensure_alias(db, "__test_dill__", product.id)
    ensure_alias(db, "__test_dill__", product.id)  # повторно

    count = db.query(ProductAlias).filter(
        ProductAlias.product_id == product.id
    ).count()
    assert count == 1


def test_match_product_trims_whitespace(db):
    """match_product игнорирует пробелы в начале/конце."""
    product = get_or_create_product(db, "__Тест_Петрушка__")
    ensure_alias(db, "__test_parsley__", product.id)

    assert match_product(db, "  __test_parsley__  ") is not None
