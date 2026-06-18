"""add warehouse_eligible to expense_categories

Revision ID: g5h6i7j8k9l0
Revises: f4a5b6c7d8e9
Branch Labels: None
Depends On: None
"""
from alembic import op
import sqlalchemy as sa

revision = 'g5h6i7j8k9l0'
down_revision = 'f4a5b6c7d8e9'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'expense_categories',
        sa.Column('warehouse_eligible', sa.Boolean(), nullable=False, server_default='false')
    )
    # Только «Продукты питания» (сырьё) идут на склад — Готовая еда нет
    op.execute(
        "UPDATE expense_categories SET warehouse_eligible = true "
        "WHERE name = 'Продукты питания'"
    )


def downgrade():
    op.drop_column('expense_categories', 'warehouse_eligible')
