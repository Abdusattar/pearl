"""sadik_portion_percent (Organization) + same_portion_for_sadik (Dish)

Рецепт хранит только школьную порцию — садик-граммовка больше не
снимок в БД (qty_sadik_g), а живой расчёт в writeoff_calc.compute_day_draft:
школа × Organization.sadik_portion_percent (default 80%), кроме блюд с
same_portion_for_sadik=True (штучная выпечка — целая порция что школе, что
садику). Backfill по имени, не id — миграция должна одинаково сработать и
на локалке, и на проде, где id блюд разные. Список из 21 названия сверен
по реальным данным (510 строк dish_ingredients, 0 необъяснённых отклонений
от 80%/1:1 после проверки).

Revision ID: x3y4z5a6b7c8
Revises: v1w2x3y4z5a6
Create Date: 2026-08-05
"""
from alembic import op
import sqlalchemy as sa

revision = 'x3y4z5a6b7c8'
down_revision = 'v1w2x3y4z5a6'
branch_labels = None
depends_on = None

SAME_PORTION_DISHES = [
    "Блинчики домашнего приготовления",
    "Булочка домашнего приготовления",
    "Булочка домашняя (без начинки, круглая)",
    "Варёное яйцо",
    "Домашний яблочный пирог (шарлотка)",
    "Запечённые пирожочки с картошкой (2 шт.)",
    "Кекс",
    "Кекс домашнего приготовления",
    "Коржики молочные",
    "Курник домашний (мини-пирог)",
    "Маффин (кекс порционный)",
    "Мини-пампушка",
    "Оромо",
    "Пирог домашний (сдобный с повидлом)",
    "Пончики домашние (выпеченные в духовке)",
    "Рулетики из теста с овощами (Ханум вегетарианский)",
    "Рулетики из теста с тыквенной начинкой (Ханум)",
    "Самсы домашнего приготовления (из слоеного теста)",
    "Шарлотка с яблоком",
    "Шоколадный кекс (с какао)",
    "Штрудли запеченные / Боорсоки духовковые",
]


def upgrade():
    op.add_column(
        'organizations',
        sa.Column('sadik_portion_percent', sa.Numeric(5, 2), nullable=False, server_default='80'),
    )
    op.add_column(
        'dishes',
        sa.Column('same_portion_for_sadik', sa.Boolean(), nullable=False, server_default='false'),
    )

    conn = op.get_bind()
    dishes_table = sa.table('dishes', sa.column('name', sa.String), sa.column('same_portion_for_sadik', sa.Boolean))
    conn.execute(
        dishes_table.update()
        .where(dishes_table.c.name.in_(SAME_PORTION_DISHES))
        .values(same_portion_for_sadik=True)
    )


def downgrade():
    op.drop_column('dishes', 'same_portion_for_sadik')
    op.drop_column('organizations', 'sadik_portion_percent')
