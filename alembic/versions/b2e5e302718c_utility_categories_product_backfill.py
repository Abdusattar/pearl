"""Коммуналка/Охрана/Реклама (ExpenseCategory) + backfill expense_category_id
для 50 товаров без статьи расходов

Категории решены 10.07 (wiki/blueprints/unit_economics_module.md), не заведены
технически до сих пор. Электричество/Вода/Отопление/Интернет — дети
существующей "Коммунальные расходы" (id=191). Охрана/Реклама — дети
существующей "Операционные услуги" (id=193). Обе родительские категории
проверены на проде (05.08) — нигде не используются напрямую как
expense_category_id товара/транзакции, добавление детей ничего не ломает.

Backfill товаров — по имени (не id), как в x3y4z5a6b7c8: 43 товара со складской
категорией (мясо/овощи/крупы/...) → "Продукты питания". 7 неоднозначных (без
category) — по контексту чека, решено с пользователем 05.08.

Revision ID: b2e5e302718c
Revises: x3y4z5a6b7c8
Create Date: 2026-08-05
"""
from alembic import op
import sqlalchemy as sa

revision = 'b2e5e302718c'
down_revision = 'x3y4z5a6b7c8'
branch_labels = None
depends_on = None

# Складские категории продуктов (Product.category), которые однозначно
# относятся к статье расходов "Продукты питания"
FOOD_STOCK_CATEGORIES = [
    "бакалея", "зелень", "крупы", "масла", "молочные",
    "мясо", "напитки", "овощи", "прочее (еда)", "фрукты", "хлеб",
]

# 7 товаров без складской категории — решено вручную по контексту чека (05.08)
AMBIGUOUS_PRODUCT_MAP = {
    "лимон": "Продукты питания",
    "кость": "Продукты питания",
    "краска для принтера": "Канцелярские товары",
    "тетрадь общий": "Канцелярские товары",
    "мыломойка": "Хозяйственные материалы",
    "увайт спирт": "Хозяйственные материалы",
    "белая река": "Бутилированная вода",
}


def upgrade():
    conn = op.get_bind()
    cat = sa.table(
        'expense_categories',
        sa.column('id', sa.Integer),
        sa.column('name', sa.String),
        sa.column('parent_id', sa.Integer),
        sa.column('warehouse_eligible', sa.Boolean),
    )

    def category_id(name: str) -> int:
        row = conn.execute(sa.select(cat.c.id).where(cat.c.name == name)).first()
        if not row:
            raise RuntimeError(f"Категория не найдена: {name}")
        return row[0]

    kommunalka_id = category_id("Коммунальные расходы")
    operatsionnye_id = category_id("Операционные услуги")

    new_categories = [
        {"name": "Электричество", "parent_id": kommunalka_id, "warehouse_eligible": False},
        {"name": "Вода", "parent_id": kommunalka_id, "warehouse_eligible": False},
        {"name": "Отопление", "parent_id": kommunalka_id, "warehouse_eligible": False},
        {"name": "Интернет", "parent_id": kommunalka_id, "warehouse_eligible": False},
        {"name": "Охрана", "parent_id": operatsionnye_id, "warehouse_eligible": False},
        {"name": "Реклама", "parent_id": operatsionnye_id, "warehouse_eligible": False},
    ]
    conn.execute(cat.insert(), new_categories)

    products = sa.table(
        'products',
        sa.column('id', sa.Integer),
        sa.column('name', sa.String),
        sa.column('category', sa.String),
        sa.column('expense_category_id', sa.Integer),
    )

    food_id = category_id("Продукты питания")
    conn.execute(
        products.update()
        .where(products.c.category.in_(FOOD_STOCK_CATEGORIES))
        .where(products.c.expense_category_id.is_(None))
        .values(expense_category_id=food_id)
    )

    for product_name, category_name in AMBIGUOUS_PRODUCT_MAP.items():
        target_id = category_id(category_name)
        conn.execute(
            products.update()
            .where(sa.func.lower(products.c.name) == product_name)
            .where(products.c.expense_category_id.is_(None))
            .values(expense_category_id=target_id)
        )


def downgrade():
    conn = op.get_bind()
    cat = sa.table('expense_categories', sa.column('id', sa.Integer), sa.column('name', sa.String))
    names = ["Электричество", "Вода", "Отопление", "Интернет", "Охрана", "Реклама"]
    conn.execute(cat.delete().where(cat.c.name.in_(names)))
    # Backfill expense_category_id намеренно не откатываем — нет надёжного
    # способа отличить "было проставлено этой миграцией" от "проставлено
    # руками после неё"; откат categories безопасен и достаточен.
