"""add external_txn_id to transactions

Revision ID: f3a4b5c6d7e8
Revises: e2f3a4b5c6d7
Create Date: 2026-06-18
"""
from alembic import op
import sqlalchemy as sa

revision = 'f3a4b5c6d7e8'
down_revision = 'e2f3a4b5c6d7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('transactions',
        sa.Column('external_txn_id', sa.String(50), nullable=True)
    )
    op.create_unique_constraint(
        'uq_transactions_external_txn_id', 'transactions', ['external_txn_id']
    )


def downgrade() -> None:
    op.drop_constraint('uq_transactions_external_txn_id', 'transactions')
    op.drop_column('transactions', 'external_txn_id')
