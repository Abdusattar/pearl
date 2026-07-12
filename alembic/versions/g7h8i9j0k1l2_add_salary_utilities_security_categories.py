"""add salary, utilities (+children), security expense categories

Revision ID: g7h8i9j0k1l2
Revises: f6g7h8i9j0k1
Create Date: 2026-07-12
"""
from alembic import op

revision = 'g7h8i9j0k1l2'
down_revision = 'f6g7h8i9j0k1'
branch_labels = None
depends_on = None


def upgrade():
    # Зарплата, Охрана — root-leaf категории-услуги (как Транспорт/Прочее/Банк.комиссии),
    # без своего каталога товаров.
    op.execute(
        "INSERT INTO expense_categories (name, parent_id, warehouse_eligible) "
        "SELECT 'Зарплата', NULL, false "
        "WHERE NOT EXISTS (SELECT 1 FROM expense_categories WHERE name = 'Зарплата' AND parent_id IS NULL)"
    )
    op.execute(
        "INSERT INTO expense_categories (name, parent_id, warehouse_eligible) "
        "SELECT 'Охрана', NULL, false "
        "WHERE NOT EXISTS (SELECT 1 FROM expense_categories WHERE name = 'Охрана' AND parent_id IS NULL)"
    )
    # Коммуналка — родитель + дочерние (видеть сезонность: зимой отопление растёт)
    op.execute(
        "INSERT INTO expense_categories (name, parent_id, warehouse_eligible) "
        "SELECT 'Коммуналка', NULL, false "
        "WHERE NOT EXISTS (SELECT 1 FROM expense_categories WHERE name = 'Коммуналка' AND parent_id IS NULL)"
    )
    for child in ("Электричество", "Вода", "Отопление", "Интернет"):
        op.execute(
            "INSERT INTO expense_categories (name, parent_id, warehouse_eligible) "
            f"SELECT '{child}', id, false FROM expense_categories "
            "WHERE name = 'Коммуналка' AND parent_id IS NULL "
            f"AND NOT EXISTS (SELECT 1 FROM expense_categories WHERE name = '{child}' "
            "AND parent_id = (SELECT id FROM expense_categories WHERE name = 'Коммуналка' AND parent_id IS NULL))"
        )


def downgrade():
    op.execute(
        "DELETE FROM expense_categories WHERE name IN ('Электричество','Вода','Отопление','Интернет') "
        "AND parent_id IN (SELECT id FROM expense_categories WHERE name = 'Коммуналка' AND parent_id IS NULL)"
    )
    op.execute("DELETE FROM expense_categories WHERE name IN ('Зарплата', 'Охрана', 'Коммуналка') AND parent_id IS NULL")
