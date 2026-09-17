"""Покупка помнит, какую версию она заменила

Revision ID: s0917a0002
Revises: s0917a0001
Create Date: 2026-09-17

«Поправить» (утверждено 17.09) — это замена: прежняя покупка уходит в историю,
как при «Убрать», новая пишется той же формой со всеми проверками. Ссылка
нужна карточке: «Поправлено: было 6 545, стало 1 347,50, прежняя версия».
"""
from alembic import op
import sqlalchemy as sa

revision = 's0917a0002'
down_revision = 's0917a0001'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('purchases', sa.Column('replaces_id', sa.Integer(), sa.ForeignKey('purchases.id'), nullable=True))


def downgrade():
    op.drop_column('purchases', 'replaces_id')
