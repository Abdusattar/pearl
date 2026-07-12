"""consolidate ФОТ/Охрана/Коммуналка into one category — распознаём по описанию
проводки, не по отдельным категориям, пока не станет понятно, что реально
часто встречается (детализируем позже, решено 12.07)

Revision ID: k1l2m3n4o5p6
Revises: j0k1l2m3n4o5
Create Date: 2026-07-12
"""
import sqlalchemy as sa
from alembic import op

revision = 'k1l2m3n4o5p6'
down_revision = 'j0k1l2m3n4o5'
branch_labels = None
depends_on = None

OLD_ROOT_NAMES = ("Зарплата", "Охрана", "Коммуналка")
OLD_CHILD_NAMES = ("Электричество", "Вода", "Отопление", "Интернет")
NEW_NAME = "Ежемесячные расходы"


def upgrade():
    conn = op.get_bind()

    conn.execute(sa.text(
        "INSERT INTO expense_categories (name, parent_id, warehouse_eligible) "
        "SELECT :name, NULL, false "
        "WHERE NOT EXISTS (SELECT 1 FROM expense_categories WHERE name = :name AND parent_id IS NULL)"
    ), {"name": NEW_NAME})

    new_id = conn.execute(sa.text(
        "SELECT id FROM expense_categories WHERE name = :name AND parent_id IS NULL"
    ), {"name": NEW_NAME}).scalar()

    # перевести все шаблоны справочника на новую категорию
    conn.execute(sa.text(
        "UPDATE recurring_expense_templates SET category_id = :new_id"
    ), {"new_id": new_id})

    # старые категории удалить, только если на них уже нет транзакций
    # (дети раньше родителей — иначе FK на parent_id мешает удалить родителя)
    old_names = OLD_CHILD_NAMES + OLD_ROOT_NAMES
    for name in old_names:
        conn.execute(sa.text(
            "DELETE FROM expense_categories WHERE name = :name "
            "AND NOT EXISTS (SELECT 1 FROM transactions WHERE category_id = expense_categories.id)"
        ), {"name": name})


def downgrade():
    # необратимо разводить обратно вручную — намеренно без авто-downgrade
    pass
