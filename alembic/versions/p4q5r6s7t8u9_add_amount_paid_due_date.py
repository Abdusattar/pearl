"""add amount_paid and due_date to transactions (кредиторка)

Revision ID: p4q5r6s7t8u9
Revises: o3p4q5r6s7t8
Create Date: 2026-07-06
"""
from alembic import op
import sqlalchemy as sa

revision = 'p4q5r6s7t8u9'
down_revision = 'o3p4q5r6s7t8'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('transactions', sa.Column('amount_paid', sa.Numeric(12, 2)))
    op.add_column('transactions', sa.Column('due_date', sa.Date()))


def downgrade():
    op.drop_column('transactions', 'due_date')
    op.drop_column('transactions', 'amount_paid')
