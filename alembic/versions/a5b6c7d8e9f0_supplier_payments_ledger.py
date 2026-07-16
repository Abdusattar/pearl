"""supplier_payments ledger + opening_balance on suppliers

Revision ID: a5b6c7d8e9f0
Revises: z4a5b6c7d8e9
Create Date: 2026-07-16
"""
from alembic import op
import sqlalchemy as sa

revision = 'a5b6c7d8e9f0'
down_revision = 'z4a5b6c7d8e9'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('suppliers', sa.Column('opening_balance', sa.Numeric(12, 2), nullable=False, server_default='0'))
    op.add_column('suppliers', sa.Column('opening_balance_date', sa.Date(), nullable=True))

    op.create_table(
        'supplier_payments',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('supplier_id', sa.Integer(), sa.ForeignKey('suppliers.id'), nullable=False),
        sa.Column('amount', sa.Numeric(12, 2), nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('comment', sa.Text(), nullable=True),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_supplier_payments_supplier_id', 'supplier_payments', ['supplier_id'])


def downgrade():
    op.drop_index('ix_supplier_payments_supplier_id', table_name='supplier_payments')
    op.drop_table('supplier_payments')
    op.drop_column('suppliers', 'opening_balance_date')
    op.drop_column('suppliers', 'opening_balance')
