"""rename expense category 'Бытовая химия' -> 'Мыломойка' (clearer name)

Revision ID: x2y3z4a5b6c7
Revises: w1x2y3z4a5b6
Create Date: 2026-07-09
"""
from alembic import op

revision = 'x2y3z4a5b6c7'
down_revision = 'w1x2y3z4a5b6'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "UPDATE expense_categories SET name = 'Мыломойка' WHERE name = 'Бытовая химия'"
    )


def downgrade():
    op.execute(
        "UPDATE expense_categories SET name = 'Бытовая химия' WHERE name = 'Мыломойка'"
    )
