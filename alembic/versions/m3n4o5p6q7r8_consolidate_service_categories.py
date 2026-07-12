"""consolidate Транспорт/Прочее/Банковские комиссии into one category —
распознаём по описанию транзакции (такси/доставка/комиссия и т.п.), не по
отдельным категориям (решено 12.07, тот же принцип, что и для
Ежемесячные расходы — детализируем позже по факту частоты)

Revision ID: m3n4o5p6q7r8
Revises: l2m3n4o5p6q7
Create Date: 2026-07-12
"""
import sqlalchemy as sa
from alembic import op

revision = 'm3n4o5p6q7r8'
down_revision = 'l2m3n4o5p6q7'
branch_labels = None
depends_on = None

OLD_NAMES = ("Транспорт", "Прочее", "Банковские комиссии")
NEW_NAME = "Сервисные расходы"


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

    # существующие проводки на старые категории переносим на новую — старых
    # позиций тут не было (проверено 0 транзакций и на локалке, и это
    # безопасно даже если на проде вдруг что-то есть: перенос, не потеря
    for name in OLD_NAMES:
        conn.execute(sa.text(
            "UPDATE transactions SET category_id = :new_id "
            "WHERE category_id = (SELECT id FROM expense_categories WHERE name = :name AND parent_id IS NULL)"
        ), {"new_id": new_id, "name": name})

    for name in OLD_NAMES:
        conn.execute(sa.text(
            "DELETE FROM expense_categories WHERE name = :name AND parent_id IS NULL"
        ), {"name": name})


def downgrade():
    # необратимо разводить обратно вручную — намеренно без авто-downgrade
    pass
