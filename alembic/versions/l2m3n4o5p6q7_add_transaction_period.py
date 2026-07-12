"""add transactions.period — "for which month", distinct from date paid

Revision ID: l2m3n4o5p6q7
Revises: k1l2m3n4o5p6
Create Date: 2026-07-12
"""
import sqlalchemy as sa
from alembic import op

revision = 'l2m3n4o5p6q7'
down_revision = 'k1l2m3n4o5p6'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('transactions', sa.Column('period', sa.Date, nullable=True))


def downgrade():
    op.drop_column('transactions', 'period')
