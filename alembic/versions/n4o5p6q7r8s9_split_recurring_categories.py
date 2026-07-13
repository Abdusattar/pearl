"""split «Ежемесячные расходы» into ФОТ/Коммунальные расходы/Связь/Операционные
услуги по типовому плану счетов КР (Класс 7 «Расходы»), по совету
финансового эксперта 14.07. Плюс новый шаблон «Газ» (коммунальные, был
пропущен) и удаление «Отопление» (0 проводок, неактуально для Сокулука).

Revision ID: n4o5p6q7r8s9
Revises: m3n4o5p6q7r8
Create Date: 2026-07-14
"""
import sqlalchemy as sa
from alembic import op

revision = 'n4o5p6q7r8s9'
down_revision = 'm3n4o5p6q7r8'
branch_labels = None
depends_on = None

PARENT_NAME = "Ежемесячные расходы"
NEW_CATEGORIES = ["ФОТ", "Коммунальные расходы", "Связь", "Операционные услуги"]

# (имя шаблона, новая категория) — Охрана -> Операционные услуги (7160, не
# 7310 административные — по рекомендации финэксперта: охрана объекта ближе
# к обслуживающим объект услугам, не к консалтингу/бухгалтерии)
TEMPLATE_CATEGORY = {
    "ФОТ (зарплата)": "ФОТ",
    "Охрана": "Операционные услуги",
    "Электричество": "Коммунальные расходы",
    "Вода": "Коммунальные расходы",
    "Интернет": "Связь",
}


def upgrade():
    conn = op.get_bind()

    parent_id = conn.execute(sa.text(
        "SELECT id FROM expense_categories WHERE name = :name AND parent_id IS NULL"
    ), {"name": PARENT_NAME}).scalar()

    for name in NEW_CATEGORIES:
        conn.execute(sa.text(
            "INSERT INTO expense_categories (name, parent_id, warehouse_eligible) "
            "SELECT :name, :parent_id, false "
            "WHERE NOT EXISTS (SELECT 1 FROM expense_categories WHERE name = :name AND parent_id = :parent_id)"
        ), {"name": name, "parent_id": parent_id})

    for tmpl_name, cat_name in TEMPLATE_CATEGORY.items():
        new_cat_id = conn.execute(sa.text(
            "SELECT id FROM expense_categories WHERE name = :name AND parent_id = :parent_id"
        ), {"name": cat_name, "parent_id": parent_id}).scalar()

        conn.execute(sa.text(
            "UPDATE recurring_expense_templates SET category_id = :cat_id WHERE name = :tmpl_name"
        ), {"cat_id": new_cat_id, "tmpl_name": tmpl_name})

        # уже проведённые проводки по этому шаблону переносим на новую категорию тоже
        conn.execute(sa.text(
            "UPDATE transactions SET category_id = :cat_id "
            "WHERE recurring_template_id = (SELECT id FROM recurring_expense_templates WHERE name = :tmpl_name)"
        ), {"cat_id": new_cat_id, "tmpl_name": tmpl_name})

    # «Отопление» — 0 проводок (проверено вручную перед миграцией), удаляем шаблон целиком
    conn.execute(sa.text("DELETE FROM recurring_expense_templates WHERE name = 'Отопление'"))

    # новый шаблон «Газ» — коммунальные, сумма своя каждый месяц (по счётчику), как Электричество/Вода
    komm_id = conn.execute(sa.text(
        "SELECT id FROM expense_categories WHERE name = 'Коммунальные расходы' AND parent_id = :parent_id"
    ), {"parent_id": parent_id}).scalar()

    conn.execute(sa.text(
        "INSERT INTO recurring_expense_templates "
        "(organization_id, name, category_id, amount_source, owner_only, active) "
        "SELECT o.id, 'Газ', :cat_id, 'manual', false, true "
        "FROM organizations o WHERE o.name = 'Садик Сокулук' "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM recurring_expense_templates t "
        "  WHERE t.organization_id = o.id AND t.name = 'Газ'"
        ")"
    ), {"cat_id": komm_id})


def downgrade():
    # необратимо разводить обратно вручную — намеренно без авто-downgrade
    pass
