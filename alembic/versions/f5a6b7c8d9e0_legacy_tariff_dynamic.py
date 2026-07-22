"""legacy_tariff — переход на динамический расчёт (без снимка на ребёнке)

Revision ID: f5a6b7c8d9e0
Revises: e9f0a1b2c3d4
Create Date: 2026-07-22
"""
from alembic import op
import sqlalchemy as sa

revision = 'f5a6b7c8d9e0'
down_revision = 'e9f0a1b2c3d4'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('organizations', sa.Column('legacy_tariff_cutoff', sa.Date(), nullable=True))
    op.add_column('organizations', sa.Column('legacy_tariff_price', sa.Numeric(10, 2), nullable=True))
    op.drop_column('students', 'legacy_tariff_amount')


def downgrade():
    op.add_column('students', sa.Column('legacy_tariff_amount', sa.Numeric(10, 2), nullable=True))
    op.drop_column('organizations', 'legacy_tariff_price')
    op.drop_column('organizations', 'legacy_tariff_cutoff')
