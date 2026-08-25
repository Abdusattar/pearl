"""Разовые услуги — is_recurring, cash_recipient, оплата+начисление одним действием

Канцелярия и подобные разовые сборы (25.08) — не месячная услуга, оплачивается
раз в год наличными, отмечается вручную (POST /services/{id}/pay), создаёт
Charge+Transaction(income) одним действием (гасят друг друга в балансе) и
CashFunding(direct_cash) в подотчёт. Organization.cash_recipient_user_id —
настройка объекта, кто физически принимает наличные (не роль, не текущий
пользователь) — NULL значит получатель тот, кто нажал кнопку.

Revision ID: d5e6f7a8b9c0
Revises: c8d9e0f1a2b3
Create Date: 2026-08-25
"""
from alembic import op
import sqlalchemy as sa

revision = 'd5e6f7a8b9c0'
down_revision = 'c8d9e0f1a2b3'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('organizations', sa.Column('cash_recipient_user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True))

    op.add_column('services', sa.Column('is_recurring', sa.Boolean(), nullable=False, server_default='true'))

    op.add_column('charges', sa.Column('deleted_at', sa.DateTime(), nullable=True))

    op.add_column('transactions', sa.Column('service_id', sa.Integer(), sa.ForeignKey('services.id'), nullable=True))
    op.add_column('transactions', sa.Column('charge_id', sa.Integer(), sa.ForeignKey('charges.id'), nullable=True))

    op.add_column('cash_fundings', sa.Column('source_transaction_id', sa.Integer(), sa.ForeignKey('transactions.id'), nullable=True))


def downgrade():
    op.drop_column('cash_fundings', 'source_transaction_id')
    op.drop_column('transactions', 'charge_id')
    op.drop_column('transactions', 'service_id')
    op.drop_column('charges', 'deleted_at')
    op.drop_column('services', 'is_recurring')
    op.drop_column('organizations', 'cash_recipient_user_id')
