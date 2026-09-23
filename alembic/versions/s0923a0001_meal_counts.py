"""Сколько сегодня едят: три числа в день и строка меню

Revision ID: s0923a0001
Revises: s0921a0002
Create Date: 2026-09-23

Склад по нормам (context/revision/12_closing_loop.md): расход продукта между
пересчётами делится на едоко-дни. Листы кухни больше не обязательны.
"""
from alembic import op
import sqlalchemy as sa

revision = 's0923a0001'
down_revision = 's0921a0002'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'meal_counts',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('site_org_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('school', sa.Integer()),
        sa.Column('sadik', sa.Integer()),
        sa.Column('staff', sa.Integer()),
        sa.Column('menu', sa.Text()),
        sa.Column('source', sa.String(10)),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id')),
        sa.Column('updated_by', sa.Integer(), sa.ForeignKey('users.id')),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint('site_org_id', 'date', name='uq_meal_count_site_date'),
    )


def downgrade():
    op.drop_table('meal_counts')
