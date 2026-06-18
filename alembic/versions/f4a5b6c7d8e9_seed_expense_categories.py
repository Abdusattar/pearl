"""seed expense categories

Revision ID: f4a5b6c7d8e9
Revises: e2f3a4b5c6d7
Branch Labels: None
Depends On: None
"""
from alembic import op

revision = 'f4a5b6c7d8e9'
down_revision = 'e2f3a4b5c6d7'
branch_labels = None
depends_on = None


def upgrade():
    # Родительские категории
    op.execute(
        "INSERT INTO expense_categories (name, parent_id, organization_id) "
        "VALUES ('Питание', NULL, NULL), "
        "       ('Хозяйство', NULL, NULL), "
        "       ('Транспорт', NULL, NULL), "
        "       ('Прочее', NULL, NULL)"
    )

    # Подкатегории Питания
    op.execute(
        "INSERT INTO expense_categories (name, parent_id, organization_id) "
        "SELECT 'Продукты питания', id, NULL FROM expense_categories "
        "WHERE name = 'Питание' AND parent_id IS NULL"
    )
    op.execute(
        "INSERT INTO expense_categories (name, parent_id, organization_id) "
        "SELECT 'Готовая еда', id, NULL FROM expense_categories "
        "WHERE name = 'Питание' AND parent_id IS NULL"
    )

    # Подкатегории Хозяйства
    op.execute(
        "INSERT INTO expense_categories (name, parent_id, organization_id) "
        "SELECT 'Бытовая химия', id, NULL FROM expense_categories "
        "WHERE name = 'Хозяйство' AND parent_id IS NULL"
    )
    op.execute(
        "INSERT INTO expense_categories (name, parent_id, organization_id) "
        "SELECT 'Канцтовары', id, NULL FROM expense_categories "
        "WHERE name = 'Хозяйство' AND parent_id IS NULL"
    )


def downgrade():
    op.execute(
        "DELETE FROM expense_categories WHERE name IN "
        "('Продукты питания', 'Готовая еда', 'Бытовая химия', 'Канцтовары')"
    )
    op.execute(
        "DELETE FROM expense_categories WHERE name IN "
        "('Питание', 'Хозяйство', 'Транспорт', 'Прочее') AND parent_id IS NULL"
    )
