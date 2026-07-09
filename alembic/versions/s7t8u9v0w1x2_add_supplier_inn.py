"""add inn to suppliers — нужен для реестров бухгалтеру (ст. 177 НК КР)

Revision ID: s7t8u9v0w1x2
Revises: r6s7t8u9v0w1
Create Date: 2026-07-09
"""
from alembic import op
import sqlalchemy as sa

revision = 's7t8u9v0w1x2'
down_revision = 'r6s7t8u9v0w1'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('suppliers', sa.Column('inn', sa.String(length=20), nullable=True))


def downgrade():
    op.drop_column('suppliers', 'inn')
