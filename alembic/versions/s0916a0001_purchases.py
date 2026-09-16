"""Покупка как одна запись: шапка `purchases`, карман и счёт на проводке, фасовка товара

Revision ID: s0916a0001
Revises: s0915a0001
Create Date: 2026-09-16

Первый экран нового входа — «Купили» (макет версии 7, блок 2б; план
`context/revision/07_buy_screen.md`). Старые таблицы остаются источником
правды: проводки по категориям, приходы на склад, долг поставщику считаются
как раньше, и старый вход видит всё, что заведено новым. Добавки рядом:

- `purchases` — шапка покупки, «одна покупка = одна строка»: у кого, когда,
  итог, как оплачено (в долг / из кассы / со счёта / часть / учредитель),
  из чьего кармана, с какого счёта, для кого (NULL = общее), фото.
- `transactions.purchase_id` — проводки одной покупки смотрят на шапку.
- `transactions.paid_from_user_id` — карман плательщика (новая Касса считает
  карманы отсюда и из cash_fundings.accountable_user_id).
- `transactions.account_org_id` — со счёта какого объекта, при paid_directly.
- `products.pack_name`, `products.pack_qty` — фасовка («лоток» = 30 шт):
  при покупке можно ввести «2 лотка», система переведёт в единицу карточки.
"""
from alembic import op
import sqlalchemy as sa

revision = 's0916a0001'
down_revision = 's0915a0001'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'purchases',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('site_org_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('supplier_id', sa.Integer(), sa.ForeignKey('suppliers.id'), nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('total', sa.Numeric(12, 2), nullable=False),
        # debt | cash | account | part | founder
        sa.Column('payment', sa.String(10), nullable=False),
        sa.Column('paid_amount', sa.Numeric(12, 2), nullable=False, server_default='0'),
        sa.Column('paid_from_user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('account_org_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=True),
        sa.Column('founder_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('funding_id', sa.Integer(), sa.ForeignKey('cash_fundings.id'), nullable=True),
        # NULL — общее (делится правилом из Настроек); иначе объект
        sa.Column('for_org_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=True),
        sa.Column('receipt_id', sa.Integer(), sa.ForeignKey('receipts.id'), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('dup_confirmed', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
    )
    op.create_index('ix_purchases_supplier_date', 'purchases', ['supplier_id', 'date'])
    op.create_index('ix_purchases_site_date', 'purchases', ['site_org_id', 'date'])

    op.add_column('transactions', sa.Column('purchase_id', sa.Integer(),
                                            sa.ForeignKey('purchases.id'), nullable=True))
    op.add_column('transactions', sa.Column('paid_from_user_id', sa.Integer(),
                                            sa.ForeignKey('users.id'), nullable=True))
    op.add_column('transactions', sa.Column('account_org_id', sa.Integer(),
                                            sa.ForeignKey('organizations.id'), nullable=True))
    op.create_index('ix_transactions_purchase', 'transactions', ['purchase_id'])

    op.add_column('products', sa.Column('pack_name', sa.String(20), nullable=True))
    op.add_column('products', sa.Column('pack_qty', sa.Numeric(10, 3), nullable=True))


def downgrade():
    op.drop_column('products', 'pack_qty')
    op.drop_column('products', 'pack_name')
    op.drop_index('ix_transactions_purchase', table_name='transactions')
    op.drop_column('transactions', 'account_org_id')
    op.drop_column('transactions', 'paid_from_user_id')
    op.drop_column('transactions', 'purchase_id')
    op.drop_index('ix_purchases_site_date', table_name='purchases')
    op.drop_index('ix_purchases_supplier_date', table_name='purchases')
    op.drop_table('purchases')
