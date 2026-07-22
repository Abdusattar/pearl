"""legacy_tariff_amount / legacy_tariff_until — временная цена переходного периода

Revision ID: e9f0a1b2c3d4
Revises: d8e9f0a1b2c3
Create Date: 2026-07-22
"""
from alembic import op
import sqlalchemy as sa

revision = 'e9f0a1b2c3d4'
down_revision = 'd8e9f0a1b2c3'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('students', sa.Column('legacy_tariff_amount', sa.Numeric(10, 2), nullable=True))
    op.add_column('organizations', sa.Column('legacy_tariff_until', sa.Date(), nullable=True))


def downgrade():
    op.drop_column('organizations', 'legacy_tariff_until')
    op.drop_column('students', 'legacy_tariff_amount')
