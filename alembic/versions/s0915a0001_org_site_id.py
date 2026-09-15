"""Площадка организации: у кого общий склад и касса

Revision ID: s0915a0001
Revises: f6a7b8c9d0e1
Create Date: 2026-09-15

Школа и Садик Сокулук — одна площадка: одна кухня, один склад, одна касса
(решение владельца 14.09, схема `context/revision/06_merge_sokuluk.md`
утверждена 15.09). Дети, сотрудники, начисления и метка объекта на расходе
остаются по объектам, а склад и касса читаются по площадке.

`site_id` — организация, у которой лежат склад и касса этой организации.
NULL значит «сама себе площадка» (Садик Сокулук, Кожомкул). Школе ставится
4 скриптом слияния, не миграцией: перенос данных показывается владельцу
как diff и пишется по его «ок». Старый вход поле не читает — площадкой
выбран org 4, по которому он и так ходит, чтобы ничего не сломать у
Махабат на время параллельной работы двух входов.
"""
from alembic import op
import sqlalchemy as sa

revision = 's0915a0001'
down_revision = 'f6a7b8c9d0e1'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('organizations', sa.Column('site_id', sa.Integer(),
                                             sa.ForeignKey('organizations.id'), nullable=True))


def downgrade():
    op.drop_column('organizations', 'site_id')
