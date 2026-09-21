"""Настройки в базе и история окладов

Revision ID: s0921a0001
Revises: s0917a0002
Create Date: 2026-09-21

Блок «Настройки» (макет 21.09): правила, зашитые в код (порог причины при
пересчёте, «пора платить», день зарплаты, ставки удержаний, рабочие дни
кухни, отложенные тарифы), переезжают в `app_settings` — владелец меняет
их сам. Сотрудники: оклад по месяцам (`employee_salaries`) и даты работы,
чтобы смена оклада и «Уволен» не переписывали прошлые ведомости.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = 's0921a0001'
down_revision = 's0917a0002'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'app_settings',
        sa.Column('key', sa.String(60), primary_key=True),
        sa.Column('value', JSONB, nullable=False),
        sa.Column('updated_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now()),
    )
    op.add_column('employees', sa.Column('started_on', sa.Date(), nullable=True))
    op.add_column('employees', sa.Column('ended_on', sa.Date(), nullable=True))
    op.create_table(
        'employee_salaries',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('employee_id', sa.Integer(), sa.ForeignKey('employees.id'), nullable=False),
        sa.Column('amount', sa.Numeric(12, 2), nullable=False),
        sa.Column('from_month', sa.Date(), nullable=False),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index('ix_employee_salaries_emp', 'employee_salaries', ['employee_id', 'from_month'])


def downgrade():
    op.drop_index('ix_employee_salaries_emp', 'employee_salaries')
    op.drop_table('employee_salaries')
    op.drop_column('employees', 'ended_on')
    op.drop_column('employees', 'started_on')
    op.drop_table('app_settings')
