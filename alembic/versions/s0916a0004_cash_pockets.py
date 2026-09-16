"""Касса с карманами: передачи между людьми, карман у изъятия, счёт у снятия

Revision ID: s0916a0004
Revises: s0916a0003
Create Date: 2026-09-16

Макет блок 4 (принят владельцем 15.09): два счёта, одна касса, три кармана
(Махабат, Мунара, Айжан). Снятие ложится в карман того, кто снял; передача —
одной записью из кармана в карман; расход — из кармана плательщика
(transactions.paid_from_user_id, 0001); пересчёт кармана — Reconciliation
kind='pocket', subject_id = users.id, новой таблицы не нужно.

- `cash_transfers` — передача наличных между карманами площадки.
- `capital_withdrawals.from_user_id` — из чьего кармана взял учредитель.
- `cash_fundings.account_org_id` — с какого счёта снято. Раньше счёт и
  касса были одним объектом; на площадке Сокулук счета два (садика и
  школы), а касса одна: снятие со счёта школы попадает в кассу площадки.
"""
from alembic import op
import sqlalchemy as sa

revision = 's0916a0004'
down_revision = 's0916a0003'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'cash_transfers',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('site_org_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('from_user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('to_user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('amount', sa.Numeric(12, 2), nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('comment', sa.Text(), nullable=True),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_cash_transfers_site_date', 'cash_transfers', ['site_org_id', 'date'])
    op.add_column('capital_withdrawals', sa.Column('from_user_id', sa.Integer(),
                                                   sa.ForeignKey('users.id'), nullable=True))
    op.add_column('cash_fundings', sa.Column('account_org_id', sa.Integer(),
                                             sa.ForeignKey('organizations.id'), nullable=True))


def downgrade():
    op.drop_column('cash_fundings', 'account_org_id')
    op.drop_column('capital_withdrawals', 'from_user_id')
    op.drop_index('ix_cash_transfers_site_date', table_name='cash_transfers')
    op.drop_table('cash_transfers')
