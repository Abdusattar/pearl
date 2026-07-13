"""group товарные категории (Питание/Хозяйство/Текущий ремонт) под новый
родитель «Товары» — по совету финэксперта (14.07): товары/материалы
(Класс 2 КР) — отдельный тип от услуг. «Капитальный ремонт» исключён и
вынесен в отдельный корень (капитализация ОС, не операционный расход, иначе
исказит сумму текущих расходов). Плюс переименования подкатегорий к
принятой в КР терминологии.

Revision ID: o5p6q7r8s9t0
Revises: n4o5p6q7r8s9
Create Date: 2026-07-14
"""
import sqlalchemy as sa
from alembic import op

revision = 'o5p6q7r8s9t0'
down_revision = 'n4o5p6q7r8s9'
branch_labels = None
depends_on = None

OLD_PARENT_NAMES = ["Питание", "Хозяйство", "Ремонт"]
NEW_PARENT_NAME = "Товары"

# старое имя категории -> новое имя (None = переехать без переименования)
MOVE_AND_RENAME = {
    "Готовая еда": "Услуги питания",
    "Продукты питания": None,
    "Питьевая вода": "Бутилированная вода",
    "Мыломойка": "Хозяйственные материалы",
    "Канцтовары": "Канцелярские товары",
    "Игрушки": None,
    "Текущий ремонт": "Текущий ремонт имущества",
}

PROMOTE_TO_ROOT = "Капитальный ремонт"


def upgrade():
    conn = op.get_bind()

    conn.execute(sa.text(
        "INSERT INTO expense_categories (name, parent_id, warehouse_eligible) "
        "SELECT :name, NULL, false "
        "WHERE NOT EXISTS (SELECT 1 FROM expense_categories WHERE name = :name AND parent_id IS NULL)"
    ), {"name": NEW_PARENT_NAME})

    new_parent_id = conn.execute(sa.text(
        "SELECT id FROM expense_categories WHERE name = :name AND parent_id IS NULL"
    ), {"name": NEW_PARENT_NAME}).scalar()

    for old_name, new_name in MOVE_AND_RENAME.items():
        if new_name:
            conn.execute(sa.text(
                "UPDATE expense_categories SET parent_id = :parent_id, name = :new_name WHERE name = :old_name"
            ), {"parent_id": new_parent_id, "new_name": new_name, "old_name": old_name})
        else:
            conn.execute(sa.text(
                "UPDATE expense_categories SET parent_id = :parent_id WHERE name = :old_name"
            ), {"parent_id": new_parent_id, "old_name": old_name})

    # «Капитальный ремонт» — на верхний уровень, CAPEX не смешиваем с товарами
    conn.execute(sa.text(
        "UPDATE expense_categories SET parent_id = NULL WHERE name = :name"
    ), {"name": PROMOTE_TO_ROOT})

    # старая корневая «Ремонт» — 1 историческая проводка (id=20, заведена до
    # разделения на Текущий/Капитальный 09.07, сумма небольшая) переносим на
    # «Текущий ремонт имущества» как более вероятный вариант
    tekushiy_id = conn.execute(sa.text(
        "SELECT id FROM expense_categories WHERE name = 'Текущий ремонт имущества'"
    )).scalar()
    old_remont_id = conn.execute(sa.text(
        "SELECT id FROM expense_categories WHERE name = 'Ремонт' AND parent_id IS NULL"
    )).scalar()
    if old_remont_id and tekushiy_id:
        conn.execute(sa.text(
            "UPDATE transactions SET category_id = :new_id WHERE category_id = :old_id"
        ), {"new_id": tekushiy_id, "old_id": old_remont_id})

    # products.expense_category_id тоже может ссылаться на старые корневые
    # категории напрямую (пропущено в первой версии — упало на проде: часть
    # товаров заведена ещё до появления подкатегорий, expense_category_id
    # указывал прямо на корень). Переносим на generic-дочернюю перед удалением.
    STRAY_PRODUCT_DEFAULT = {
        "Питание": "Продукты питания",
        "Хозяйство": "Хозяйственные материалы",
        "Ремонт": "Текущий ремонт имущества",
    }
    for old_name, default_child in STRAY_PRODUCT_DEFAULT.items():
        old_id = conn.execute(sa.text(
            "SELECT id FROM expense_categories WHERE name = :name AND parent_id IS NULL"
        ), {"name": old_name}).scalar()
        if not old_id:
            continue
        default_child_id = conn.execute(sa.text(
            "SELECT id FROM expense_categories WHERE name = :name AND parent_id = :parent_id"
        ), {"name": default_child, "parent_id": new_parent_id}).scalar()
        if default_child_id:
            conn.execute(sa.text(
                "UPDATE products SET expense_category_id = :new_id WHERE expense_category_id = :old_id"
            ), {"new_id": default_child_id, "old_id": old_id})

    # старые пустые родители (Питание/Хозяйство/Ремонт) удаляем — теперь без
    # детей, без прямых проводок и без товаров. NOT EXISTS на products —
    # доп. страховка: если всё же что-то осталось, родитель просто не
    # удалится (безвредный осиротевший корень), а не уронит всю миграцию.
    for name in OLD_PARENT_NAMES:
        conn.execute(sa.text(
            "DELETE FROM expense_categories "
            "WHERE name = :name AND parent_id IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM transactions WHERE category_id = expense_categories.id) "
            "AND NOT EXISTS (SELECT 1 FROM products WHERE expense_category_id = expense_categories.id) "
            "AND NOT EXISTS (SELECT 1 FROM expense_categories c2 WHERE c2.parent_id = expense_categories.id)"
        ), {"name": name})


def downgrade():
    # необратимо разводить обратно вручную — намеренно без авто-downgrade
    pass
