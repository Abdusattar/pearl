"""add expense_category_id to products + backfill from product.category

Revision ID: q5r6s7t8u9v0
Revises: p4q5r6s7t8u9
Create Date: 2026-07-06
"""
from alembic import op
import sqlalchemy as sa

revision = 'q5r6s7t8u9v0'
down_revision = 'p4q5r6s7t8u9'
branch_labels = None
depends_on = None

# мелкая категория товара (Product.category) -> название статьи расходов (ExpenseCategory.name)
CATEGORY_MAP = {
    'бакалея': 'Продукты питания',
    'зелень': 'Продукты питания',
    'крупы': 'Продукты питания',
    'масла': 'Продукты питания',
    'молочные': 'Продукты питания',
    'мясо': 'Продукты питания',
    'напитки': 'Продукты питания',
    'овощи': 'Продукты питания',
    'прочее (еда)': 'Продукты питания',
    'специи': 'Продукты питания',
    'фрукты': 'Продукты питания',
    'хлеб': 'Продукты питания',
    'бытовая химия': 'Бытовая химия',
    'игрушки': 'Игрушки',
    'инвентарь': 'Хозяйство',
    'стройматериалы': 'Ремонт',
}


def upgrade():
    op.add_column(
        'products',
        sa.Column('expense_category_id', sa.Integer(), sa.ForeignKey('expense_categories.id')),
    )
    conn = op.get_bind()
    for product_category, expense_category_name in CATEGORY_MAP.items():
        conn.execute(
            sa.text(
                "UPDATE products SET expense_category_id = "
                "(SELECT id FROM expense_categories WHERE name = :cat_name) "
                "WHERE category = :prod_cat"
            ),
            {"cat_name": expense_category_name, "prod_cat": product_category},
        )


def downgrade():
    op.drop_column('products', 'expense_category_id')
