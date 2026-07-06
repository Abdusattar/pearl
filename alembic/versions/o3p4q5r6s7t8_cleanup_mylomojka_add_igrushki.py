"""remove unused mylomojka category, add igrushki subcategory

Revision ID: o3p4q5r6s7t8
Revises: n2o3p4q5r6s7
Create Date: 2026-07-06
"""
from alembic import op

revision = 'o3p4q5r6s7t8'
down_revision = 'n2o3p4q5r6s7'
branch_labels = None
depends_on = None


def upgrade():
    # «Мыломойка» (root, добавлена 29.06) дублирует «Хозяйство → Бытовая химия»
    # и не использовалась ни одной транзакцией — безопасно удалить.
    op.execute(
        "DELETE FROM expense_categories "
        "WHERE name = 'Мыломойка' AND parent_id IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM transactions WHERE transactions.category_id = expense_categories.id)"
    )
    op.execute(
        "INSERT INTO expense_categories (name, parent_id, warehouse_eligible) "
        "SELECT 'Игрушки', id, false FROM expense_categories "
        "WHERE name = 'Хозяйство' AND parent_id IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM expense_categories WHERE name = 'Игрушки')"
    )


def downgrade():
    op.execute(
        "DELETE FROM expense_categories WHERE name = 'Игрушки' "
        "AND parent_id IN (SELECT id FROM expense_categories WHERE name = 'Хозяйство' AND parent_id IS NULL)"
    )
