"""Подотчёт — cash_fundings, account_balance_snapshots, transactions.paid_directly

Механизм подотчётных средств (25.08, спроектировано в сессии). Пополнение
(снятие со счёта либо наличными напрямую) открывает пул денег на руках,
привязанный к organization_id; расходы (Transaction.paid_directly=False,
по умолчанию) списываются с него сами, FIFO по дате — считается на лету в
app/services/podotchet.py, тот же приём, что supplier_ledger.py. Сверка
остатка по счёту — через отдельные точки (account_balance_snapshots), без
попытки восстановить историю до первой введённой.

Revision ID: c8d9e0f1a2b3
Revises: b2e5e302718c
Create Date: 2026-08-25
"""
from alembic import op
import sqlalchemy as sa

revision = 'c8d9e0f1a2b3'
down_revision = 'b2e5e302718c'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('transactions', sa.Column('paid_directly', sa.Boolean(), nullable=False, server_default='false'))

    op.create_table(
        'cash_fundings',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('organization_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('source_type', sa.String(20), nullable=False),
        sa.Column('amount', sa.Numeric(12, 2), nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('taken_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('accountable_user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('source_organization_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=True),
        sa.Column('comment', sa.Text(), nullable=True),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_cash_fundings_org', 'cash_fundings', ['organization_id'])

    op.create_table(
        'account_balance_snapshots',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('organization_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('balance', sa.Numeric(12, 2), nullable=False),
        sa.Column('comment', sa.Text(), nullable=True),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index('ix_account_balance_snapshots_org_date', 'account_balance_snapshots', ['organization_id', 'date'])


def downgrade():
    op.drop_index('ix_account_balance_snapshots_org_date', table_name='account_balance_snapshots')
    op.drop_table('account_balance_snapshots')
    op.drop_index('ix_cash_fundings_org', table_name='cash_fundings')
    op.drop_table('cash_fundings')
    op.drop_column('transactions', 'paid_directly')
