"""Капитал учредителей — cash_fundings.source_founder_id, capital_withdrawals

Взнос учредителя наличными в кассу — расширение cash_fundings (тот же
приём, что source_organization_id для перетоков между бизнесами, только
источник — конкретный человек с role=founder). Изъятие учредителя — новая
таблица capital_withdrawals, зеркало cash_fundings, но уменьшает пул
подотчёта вместо пополнения. Оба не Transaction — не попадают в P&L/
категории/юнит-экономику (совет финэксперта 02.09: капитал, не доход/
расход бизнеса).

Revision ID: e1f2a3b4c5d6
Revises: d5e6f7a8b9c0
Create Date: 2026-09-02
"""
from alembic import op
import sqlalchemy as sa

revision = 'e1f2a3b4c5d6'
down_revision = 'd5e6f7a8b9c0'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('cash_fundings', sa.Column('source_founder_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True))

    op.create_table(
        'capital_withdrawals',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('organization_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('founder_user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('amount', sa.Numeric(12, 2), nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('comment', sa.Text(), nullable=True),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_capital_withdrawals_org', 'capital_withdrawals', ['organization_id'])


def downgrade():
    op.drop_index('ix_capital_withdrawals_org', table_name='capital_withdrawals')
    op.drop_table('capital_withdrawals')
    op.drop_column('cash_fundings', 'source_founder_id')
