"""add bank fees expense category

Revision ID: f6g7h8i9j0k1
Revises: e5f6g7h8i9j0
Create Date: 2026-07-12
"""
from alembic import op

revision = 'f6g7h8i9j0k1'
down_revision = 'e5f6g7h8i9j0'
branch_labels = None
depends_on = None


def upgrade():
    # Рекомендация финэксперта 06.07 (комиссии Optima и т.п., счёт 7810) —
    # родительская категория-услуга, как Транспорт/Прочее (без своего каталога товаров).
    op.execute(
        "INSERT INTO expense_categories (name, parent_id, warehouse_eligible) "
        "SELECT 'Банковские комиссии', NULL, false "
        "WHERE NOT EXISTS (SELECT 1 FROM expense_categories WHERE name = 'Банковские комиссии' AND parent_id IS NULL)"
    )


def downgrade():
    op.execute(
        "DELETE FROM expense_categories WHERE name = 'Банковские комиссии' AND parent_id IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM transactions WHERE transactions.category_id = expense_categories.id)"
    )
