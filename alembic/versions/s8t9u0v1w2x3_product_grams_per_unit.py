"""products.grams_per_unit — вес 1 шт. для авто-списания по рецептуре (штучные товары)

Revision ID: s8t9u0v1w2x3
Revises: r7s8t9u0v1w2
Create Date: 2026-07-27
"""
from alembic import op
import sqlalchemy as sa

revision = 's8t9u0v1w2x3'
down_revision = 'r7s8t9u0v1w2'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('products', sa.Column('grams_per_unit', sa.Numeric(10, 2), nullable=True))


def downgrade():
    op.drop_column('products', 'grams_per_unit')
