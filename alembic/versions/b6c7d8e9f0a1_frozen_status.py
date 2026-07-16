"""organizations.frozen_discount_percent — настройка статуса "Заморожен"

Revision ID: b6c7d8e9f0a1
Revises: a5b6c7d8e9f0
Create Date: 2026-07-16
"""
from alembic import op
import sqlalchemy as sa

revision = 'b6c7d8e9f0a1'
down_revision = 'a5b6c7d8e9f0'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'organizations',
        sa.Column('frozen_discount_percent', sa.Numeric(5, 2), nullable=False, server_default='50'),
    )


def downgrade():
    op.drop_column('organizations', 'frozen_discount_percent')
