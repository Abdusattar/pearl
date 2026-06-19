"""fix expense categories idempotent cleanup

Revision ID: i7j8k9l0m1n2
Revises: h6i7j8k9l0m1
Create Date: 2026-06-19 12:57:19.805403
"""
from alembic import op

revision = 'i7j8k9l0m1n2'
down_revision = 'h6i7j8k9l0m1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Добавить недостающие ROOT
    op.execute(
        "INSERT INTO expense_categories (name, parent_id, warehouse_eligible) "
        "SELECT 'Ремонт', NULL, false "
        "WHERE NOT EXISTS (SELECT 1 FROM expense_categories WHERE name='Ремонт' AND parent_id IS NULL)"
    )

    # 2. Добавить недостающие подкатегории Питания
    op.execute(
        "INSERT INTO expense_categories (name, parent_id, warehouse_eligible) "
        "SELECT 'Питьевая вода', id, false FROM expense_categories "
        "WHERE name='Питание' AND parent_id IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM expense_categories WHERE name='Питьевая вода')"
    )

    # 3. Удалить дубли — перенести транзакции и дочерних на минимальный id
    op.execute("""
        UPDATE transactions SET category_id = c_keep.id
        FROM expense_categories c_dup
        JOIN (
            SELECT name,
                   COALESCE(parent_id::text, 'NULL') AS parent_key,
                   MIN(id) AS id
            FROM expense_categories
            GROUP BY name, COALESCE(parent_id::text, 'NULL')
        ) c_keep ON c_keep.name = c_dup.name
            AND COALESCE(c_dup.parent_id::text, 'NULL') = c_keep.parent_key
        WHERE transactions.category_id = c_dup.id
          AND c_dup.id != c_keep.id
    """)
    op.execute("""
        UPDATE expense_categories child
        SET parent_id = c_keep.id
        FROM expense_categories c_dup
        JOIN (
            SELECT name,
                   COALESCE(parent_id::text, 'NULL') AS parent_key,
                   MIN(id) AS id
            FROM expense_categories
            GROUP BY name, COALESCE(parent_id::text, 'NULL')
        ) c_keep ON c_keep.name = c_dup.name
            AND COALESCE(c_dup.parent_id::text, 'NULL') = c_keep.parent_key
        WHERE child.parent_id = c_dup.id
          AND c_dup.id != c_keep.id
    """)
    op.execute("""
        DELETE FROM expense_categories
        WHERE id NOT IN (
            SELECT MIN(id) FROM expense_categories
            GROUP BY name, COALESCE(parent_id::text, 'NULL')
        )
    """)

    # 4. Удалить категории вне области Pearl (Зарплаты → 1С, Коммунальные, Канцелярия)
    op.execute("""
        UPDATE transactions SET category_id = (
            SELECT id FROM expense_categories
            WHERE name='Прочее' AND parent_id IS NULL LIMIT 1
        )
        WHERE category_id IN (
            SELECT id FROM expense_categories
            WHERE name IN ('Зарплаты', 'Коммунальные', 'Канцелярия') AND parent_id IS NULL
        )
    """)
    op.execute(
        "DELETE FROM expense_categories "
        "WHERE name IN ('Зарплаты', 'Коммунальные', 'Канцелярия') AND parent_id IS NULL"
    )

    # 5. Гарантировать warehouse_eligible на Продукты питания
    op.execute(
        "UPDATE expense_categories SET warehouse_eligible = true "
        "WHERE name = 'Продукты питания'"
    )


def downgrade() -> None:
    pass
