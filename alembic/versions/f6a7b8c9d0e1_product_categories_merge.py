"""Категории товаров с уровнем, слияние и вывод карточек: точка отсчёта нового входа

Revision ID: f6a7b8c9d0e1
Revises: c4d5e6f7a8b9
Create Date: 2026-09-15

Макет 3г (принят владельцем 15.09): два уровня товаров. Основные числятся по
остатку, мелочь уходит в расход в день покупки. Уровень задаёт категория, а не
человек: список категорий-мелочи задаёт собственник один раз, при новом товаре
выбираются только категория и единица. Поэтому категория становится таблицей с
уровнем, а не строкой на карточке (строка `products.category` остаётся до
закрытия старого входа, старые экраны читают её).

`merged_into_id` — карточка слита в другую (лук ×4, тряпки ×4 и т.д.): история
переносится на цель, старая карточка остаётся указателем, чтобы ссылки и
поиск по старому имени вели на цель. `retired_at` — карточка убрана из
каталога, потому что это не товар (такси, ключ-дубликат): строки чеков и
расход остаются, на складе и в поиске её нет.
"""
from alembic import op
import sqlalchemy as sa

revision = 'f6a7b8c9d0e1'
down_revision = 'c4d5e6f7a8b9'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'product_categories',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('name', sa.String(50), nullable=False, unique=True),
        # stock — основной, числится по остатку; minor — мелочь, в расход сразу
        sa.Column('level', sa.String(10), nullable=False, server_default='stock'),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default='100'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
    )
    op.add_column('products', sa.Column('category_id', sa.Integer(),
                                        sa.ForeignKey('product_categories.id'), nullable=True))
    op.add_column('products', sa.Column('merged_into_id', sa.Integer(),
                                        sa.ForeignKey('products.id'), nullable=True))
    op.add_column('products', sa.Column('retired_at', sa.DateTime(), nullable=True))
    op.create_index('ix_products_category', 'products', ['category_id'])


def downgrade():
    op.drop_index('ix_products_category', table_name='products')
    op.drop_column('products', 'retired_at')
    op.drop_column('products', 'merged_into_id')
    op.drop_column('products', 'category_id')
    op.drop_table('product_categories')
