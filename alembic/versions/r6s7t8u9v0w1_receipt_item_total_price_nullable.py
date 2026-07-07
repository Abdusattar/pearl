"""allow receipt_items.total_price to be null (позиция без цены — дозаполняется вручную)

Revision ID: r6s7t8u9v0w1
Revises: q5r6s7t8u9v0
Create Date: 2026-07-07
"""
from alembic import op
import sqlalchemy as sa

revision = 'r6s7t8u9v0w1'
down_revision = 'q5r6s7t8u9v0'
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column('receipt_items', 'total_price', existing_type=sa.Numeric(12, 2), nullable=True)


def downgrade():
    op.execute("UPDATE receipt_items SET total_price = 0 WHERE total_price IS NULL")
    op.alter_column('receipt_items', 'total_price', existing_type=sa.Numeric(12, 2), nullable=False)
