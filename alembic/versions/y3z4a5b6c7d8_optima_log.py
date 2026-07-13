"""Лог всех попыток Optima (check/pay), включая отклонённые — не только
успешные Transaction. Нужен, чтобы не смотреть логи Railway через CLI на
каждый тестовый/боевой платёж (запрос Абдусаттара, 13.07). Видно только
системному интегратору — страница закрыта по user_id, не по роли.

Revision ID: y3z4a5b6c7d8
Revises: q7r8s9t0u1v2
Create Date: 2026-07-13
"""
import sqlalchemy as sa
from alembic import op

revision = 'y3z4a5b6c7d8'
down_revision = 'q7r8s9t0u1v2'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'optima_log',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('command', sa.String(length=20)),
        sa.Column('account', sa.String(length=20)),
        sa.Column('txn_id', sa.String(length=50)),
        sa.Column('sum', sa.String(length=20)),
        sa.Column('result_code', sa.Integer(), nullable=False),
        sa.Column('comment', sa.Text()),
        sa.Column('client_ip', sa.String(length=50)),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()')),
    )
    op.create_index('ix_optima_log_created_at', 'optima_log', ['created_at'])


def downgrade():
    op.drop_index('ix_optima_log_created_at', table_name='optima_log')
    op.drop_table('optima_log')
