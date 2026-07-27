"""dish_ingredients — рецептура блюда (граммы на порцию, садик/школа) для авто-расчёта списания

Revision ID: a1b2c3d4e5f6
Revises: f5a6b7c8d9e0
Create Date: 2026-07-27
"""
from alembic import op
import sqlalchemy as sa

revision = 'r7s8t9u0v1w2'
down_revision = 'f5a6b7c8d9e0'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'dish_ingredients',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('dish_id', sa.Integer(), sa.ForeignKey('dishes.id'), nullable=False),
        # nullable — на момент разового импорта из тех.карты часть сырья ещё не заведена
        # как Product на складе (появится после инвентаризации), но саму норму грамм
        # терять не хотим — raw_name хранит исходное название для последующей привязки.
        sa.Column('product_id', sa.Integer(), sa.ForeignKey('products.id'), nullable=True),
        sa.Column('raw_name', sa.String(200), nullable=False),
        sa.Column('qty_sadik_g', sa.Numeric(10, 2), nullable=True),
        sa.Column('qty_shkola_g', sa.Numeric(10, 2), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index('ix_dish_ingredients_dish_id', 'dish_ingredients', ['dish_id'])


def downgrade():
    op.drop_index('ix_dish_ingredients_dish_id', table_name='dish_ingredients')
    op.drop_table('dish_ingredients')
