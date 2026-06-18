"""add warehouse module: receipts, write_offs, product unit/category

Revision ID: b5c6d7e8f9a0
Revises: a1b2c3d4e5f6
Create Date: 2026-06-18 14:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = 'b5c6d7e8f9a0'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('products', sa.Column('unit', sa.String(10), nullable=True))
    op.add_column('products', sa.Column('category', sa.String(50), nullable=True))

    op.execute("UPDATE products SET unit = 'кг' WHERE unit IS NULL")

    op.create_table(
        'warehouse_receipts',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('product_id', sa.Integer(), sa.ForeignKey('products.id'), nullable=False),
        sa.Column('quantity', sa.Numeric(10, 3), nullable=False),
        sa.Column('price_per_unit', sa.Numeric(10, 2), nullable=False),
        sa.Column('total_cost', sa.Numeric(12, 2), nullable=False),
        sa.Column('organization_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('supplier_name', sa.String(200)),
        sa.Column('transaction_id', sa.Integer(), sa.ForeignKey('transactions.id'), nullable=True),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id')),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('deleted_at', sa.DateTime()),
    )

    op.create_table(
        'write_offs',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('product_id', sa.Integer(), sa.ForeignKey('products.id'), nullable=False),
        sa.Column('quantity', sa.Numeric(10, 3), nullable=False),
        sa.Column('organization_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('children_count', sa.Integer()),
        sa.Column('reason', sa.String(100), server_default='питание детей'),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id')),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('deleted_at', sa.DateTime()),
    )


def downgrade():
    op.drop_table('write_offs')
    op.drop_table('warehouse_receipts')
    op.drop_column('products', 'category')
    op.drop_column('products', 'unit')
