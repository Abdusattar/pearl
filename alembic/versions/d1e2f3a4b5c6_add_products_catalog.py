"""add products catalog and product_aliases

Revision ID: d1e2f3a4b5c6
Revises: c3d4e5f6a1b2
Create Date: 2026-06-15 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = 'd1e2f3a4b5c6'
down_revision = 'c3d4e5f6a1b2'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'products',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('name', sa.String(100), nullable=False, unique=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
    )

    op.create_table(
        'product_aliases',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('raw_text', sa.String(200), nullable=False, unique=True),
        sa.Column('product_id', sa.Integer(), sa.ForeignKey('products.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
    )

    op.add_column('receipt_items',
        sa.Column('product_id', sa.Integer(), sa.ForeignKey('products.id'), nullable=True)
    )


def downgrade():
    op.drop_column('receipt_items', 'product_id')
    op.drop_table('product_aliases')
    op.drop_table('products')
