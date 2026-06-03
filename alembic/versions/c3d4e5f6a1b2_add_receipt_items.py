"""add receipt_items table

Revision ID: c3d4e5f6a1b2
Revises: 5bc980f71e2f
Create Date: 2026-06-02 10:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'c3d4e5f6a1b2'
down_revision = '5bc980f71e2f'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'receipt_items',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('receipt_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('qty', sa.Numeric(precision=10, scale=3), nullable=True),
        sa.Column('unit_price', sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column('total_price', sa.Numeric(precision=12, scale=2), nullable=False),
        sa.ForeignKeyConstraint(['receipt_id'], ['receipts.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_receipt_items_receipt_id', 'receipt_items', ['receipt_id'])


def downgrade() -> None:
    op.drop_index('ix_receipt_items_receipt_id', table_name='receipt_items')
    op.drop_table('receipt_items')
