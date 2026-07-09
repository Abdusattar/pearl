"""split "Ремонт" into "Текущий ремонт" / "Капитальный ремонт" subcategories —
по ответу финэксперта: текущий ремонт = расход периода, капитальный/реконструкция
= капитализация в стоимость здания. Существующие товары "Ремонт" по умолчанию
переезжают на "Текущий ремонт" (частый случай) — капитальные переклассифицируются
вручную через редактирование транзакции, когда такое случается.

Revision ID: u9v0w1x2y3z4
Revises: t8u9v0w1x2y3
Create Date: 2026-07-09
"""
from alembic import op

revision = 'u9v0w1x2y3z4'
down_revision = 't8u9v0w1x2y3'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "INSERT INTO expense_categories (name, parent_id, warehouse_eligible) "
        "SELECT 'Текущий ремонт', id, false FROM expense_categories "
        "WHERE name = 'Ремонт' AND parent_id IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM expense_categories WHERE name = 'Текущий ремонт')"
    )
    op.execute(
        "INSERT INTO expense_categories (name, parent_id, warehouse_eligible) "
        "SELECT 'Капитальный ремонт', id, false FROM expense_categories "
        "WHERE name = 'Ремонт' AND parent_id IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM expense_categories WHERE name = 'Капитальный ремонт')"
    )
    # Товары, ранее закреплённые прямо за корневой "Ремонт" — переезжают на
    # "Текущий ремонт" по умолчанию (родительская категория перестаёт быть
    # листовой, как и у "Питание"/"Хозяйство").
    op.execute(
        "UPDATE products SET expense_category_id = ("
        "  SELECT c.id FROM expense_categories c "
        "  JOIN expense_categories p ON c.parent_id = p.id "
        "  WHERE c.name = 'Текущий ремонт' AND p.name = 'Ремонт' AND p.parent_id IS NULL"
        ") "
        "WHERE expense_category_id = ("
        "  SELECT id FROM expense_categories WHERE name = 'Ремонт' AND parent_id IS NULL"
        ")"
    )


def downgrade():
    remont_id_sql = "(SELECT id FROM expense_categories WHERE name = 'Ремонт' AND parent_id IS NULL)"

    update_sql = (
        f"UPDATE products SET expense_category_id = {remont_id_sql} "
        "WHERE expense_category_id IN ("
        "  SELECT id FROM expense_categories WHERE name IN ('Текущий ремонт', 'Капитальный ремонт') "
        f"  AND parent_id = {remont_id_sql}"
        ")"
    )
    op.execute(update_sql)

    delete_sql = (
        "DELETE FROM expense_categories WHERE name IN ('Текущий ремонт', 'Капитальный ремонт') "
        f"AND parent_id = {remont_id_sql}"
    )
    op.execute(delete_sql)
