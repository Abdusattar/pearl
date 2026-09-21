"""Черновик из чата: вид, источник, что распознано, во что превратился

Revision ID: s0921a0002
Revises: s0921a0001
Create Date: 2026-09-21

Модуль «бот → черновик → проверка Махабат» (context/revision/11_bot_inbox.md):
всё, что прислали в чат, — строка `receipts` (как фото чека сейчас), теперь
любого вида. Ничего не пишет в деньги и склад, пока Махабат не внесёт формой.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = 's0921a0002'
down_revision = 's0921a0001'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('receipts', sa.Column('kind', sa.String(20), nullable=False, server_default='receipt'))
    op.add_column('receipts', sa.Column('source', sa.String(20), nullable=True))
    op.add_column('receipts', sa.Column('payload', JSONB, nullable=True))
    op.add_column('receipts', sa.Column('result_type', sa.String(30), nullable=True))
    op.add_column('receipts', sa.Column('result_id', sa.Integer(), nullable=True))
    op.add_column('receipts', sa.Column('decided_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True))
    op.add_column('receipts', sa.Column('decided_at', sa.DateTime(), nullable=True))
    op.add_column('receipts', sa.Column('reject_reason', sa.Text(), nullable=True))


def downgrade():
    for c in ('reject_reason', 'decided_at', 'decided_by', 'result_id', 'result_type', 'payload', 'source', 'kind'):
        op.drop_column('receipts', c)
