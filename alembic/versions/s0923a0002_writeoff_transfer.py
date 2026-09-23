"""Передача продуктов в другой садик: куда и по какой цене

Revision ID: s0923a0002
Revises: s0923a0001
Create Date: 2026-09-23

Сокулук передал Кожомкулу 12 л молока (23.09). Это не расход кухни: в нормы на
едока не идёт, стоимость — по цене закупки — ложится на садик-получатель
(решение владельца: без долга между точками, раскладка затрат).
"""
from alembic import op
import sqlalchemy as sa

revision = 's0923a0002'
down_revision = 's0923a0001'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('write_offs', sa.Column('to_org_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=True))
    op.add_column('write_offs', sa.Column('unit_cost', sa.Numeric(12, 2), nullable=True))


def downgrade():
    op.drop_column('write_offs', 'unit_cost')
    op.drop_column('write_offs', 'to_org_id')
