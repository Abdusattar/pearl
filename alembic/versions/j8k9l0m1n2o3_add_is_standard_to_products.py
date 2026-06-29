"""add is_standard to products

Revision ID: j8k9l0m1n2o3
Revises: i7j8k9l0m1n2
Create Date: 2026-06-27
"""
from alembic import op
import sqlalchemy as sa

revision = 'j8k9l0m1n2o3'
down_revision = 'i7j8k9l0m1n2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('products', sa.Column('is_standard', sa.Boolean(), nullable=False, server_default='false'))


def downgrade() -> None:
    op.drop_column('products', 'is_standard')
