"""Оплата поставщику: откуда деньги (карман или счёт), чтобы платёж уменьшал кассу

Revision ID: s0916a0003
Revises: s0916a0002
Create Date: 2026-09-16

В старом входе платёж поставщику (SupplierPayment) уменьшал только долг:
касса (`podotchet._spent_pool`) считала одни проводки, и «отдали Халиме
20 000 из кассы» на остаток кассы не влияло. Новый экран «Оплатить»
(макет 2в/2д) записывает, из чьего кармана или с какого счёта ушли деньги.
Старые платежи с organization_id NULL в кассу не входят — история не
двигается.
"""
from alembic import op
import sqlalchemy as sa

revision = 's0916a0003'
down_revision = 's0916a0002'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('supplier_payments', sa.Column('organization_id', sa.Integer(),
                                                 sa.ForeignKey('organizations.id'), nullable=True))
    op.add_column('supplier_payments', sa.Column('paid_from_user_id', sa.Integer(),
                                                 sa.ForeignKey('users.id'), nullable=True))
    op.add_column('supplier_payments', sa.Column('account_org_id', sa.Integer(),
                                                 sa.ForeignKey('organizations.id'), nullable=True))
    op.add_column('supplier_payments', sa.Column('paid_directly', sa.Boolean(), nullable=False,
                                                 server_default='false'))


def downgrade():
    op.drop_column('supplier_payments', 'paid_directly')
    op.drop_column('supplier_payments', 'account_org_id')
    op.drop_column('supplier_payments', 'paid_from_user_id')
    op.drop_column('supplier_payments', 'organization_id')
