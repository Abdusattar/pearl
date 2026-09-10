"""Фото бумажного листа как приложение к пересчёту: stock_count_photos

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
Create Date: 2026-09-10

Пересчёт физически делают на складе с тетрадью, а в систему цифры переносят
позже. Без снимка листа результат — число без основания: через месяц не
проверить, откуда взялось «сахар 1 270 кг», и спор упирается в память людей.
Снимок играет ту же роль, что фото чека при закупе.

Отдельная таблица, а не колонка на сессии: первый же реальный пересчёт
(09.09.2026, Садик Сокулук) пришёл на двух листах.
"""
from alembic import op
import sqlalchemy as sa

revision = 'c4d5e6f7a8b9'
down_revision = 'b3c4d5e6f7a8'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'stock_count_photos',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('count_id', sa.Integer(),
                  sa.ForeignKey('stock_counts.id', ondelete='CASCADE'), nullable=False),
        sa.Column('file_path', sa.String(500), nullable=False),
        sa.Column('caption', sa.String(200)),
        sa.Column('uploaded_by', sa.Integer(), sa.ForeignKey('users.id')),
        sa.Column('uploaded_at', sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index('ix_stock_count_photos_count', 'stock_count_photos', ['count_id'])


def downgrade():
    op.drop_index('ix_stock_count_photos_count', table_name='stock_count_photos')
    op.drop_table('stock_count_photos')
