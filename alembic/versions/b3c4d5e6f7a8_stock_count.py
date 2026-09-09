"""Пересчёт склада как сессия: stock_counts + stock_count_lines

Revision ID: b3c4d5e6f7a8
Revises: a2b3c4d5e6f7
Create Date: 2026-09-09

Актуализация склада была одной формой: заполнила — нажала «Сохранить». Для
159 позиций это не работает. Махабат обходит склад с телефоном, считая мешки
и коробки; любое прерывание — погас экран, звонок, «назад» — обнуляло всё
введённое, и второй раз за это никто не сядет.

Второе, чего форма не давала: ответа на прямой вопрос владельца «прошлись ли
по каждому продукту». Позиция, где факт совпал с системой, не оставляла в базе
никакого следа — назавтра видны только расхождения.

Отсюда сессия. Строки заводятся на все товары сразу при старте — тогда «прошли
159 из 159» это ответ из базы, и видно, по какому именно списку прошли (состав
не плывёт, если за время обхода из чека появится новый товар). Остатки меняются
только на «Завершить»: брошенная сессия ничего не портит.

`expected_qty` — снимок остатка на момент отметки, по образцу
Reconciliation.expected_amount: если за время обхода по товару прошло движение,
на завершении это видно, а не проглатывается молча.
"""
from alembic import op
import sqlalchemy as sa

revision = 'b3c4d5e6f7a8'
down_revision = 'a2b3c4d5e6f7'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'stock_counts',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('organization_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('count_date', sa.Date(), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='active'),
        sa.Column('started_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('started_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('applied_by', sa.Integer(), sa.ForeignKey('users.id')),
        sa.Column('applied_at', sa.DateTime()),
        sa.Column('cancelled_by', sa.Integer(), sa.ForeignKey('users.id')),
        sa.Column('cancelled_at', sa.DateTime()),
        sa.Column('cancel_reason', sa.Text()),
    )
    # Одна активная сессия на объект — иначе двое посчитают одно и то же
    # по-разному, и чей результат применится, будет решать порядок кликов.
    op.create_index(
        'ix_stock_counts_one_active', 'stock_counts', ['organization_id'],
        unique=True, postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index('ix_stock_counts_org_status', 'stock_counts', ['organization_id', 'status'])

    op.create_table(
        'stock_count_lines',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('count_id', sa.Integer(),
                  sa.ForeignKey('stock_counts.id', ondelete='CASCADE'), nullable=False),
        sa.Column('product_id', sa.Integer(), sa.ForeignKey('products.id'), nullable=False),
        # Остаток по системе на момент отметки — снимок, не пересчитывается.
        sa.Column('expected_qty', sa.Numeric(12, 3)),
        # NULL = позиция ещё не отмечена. Именно это отличает «насчитали ноль»
        # от «не дошли» — в старой форме и то и другое выглядело пустой строкой.
        sa.Column('actual_qty', sa.Numeric(12, 3)),
        sa.Column('mode', sa.String(10)),  # same|zero|number
        sa.Column('marked_by', sa.Integer(), sa.ForeignKey('users.id')),
        sa.Column('marked_at', sa.DateTime()),
        # Сигнал «тут что-то не так» — единица, дубль, товара нет. Карточку
        # товара при этом не меняем: смена единицы задним числом переписывает
        # смысл всей прошлой истории (Яйцо покупали лотками, списывают штуками),
        # это разбирается отдельно, а не на обходе склада.
        sa.Column('issue', sa.String(20)),
        sa.Column('note', sa.Text()),
        sa.UniqueConstraint('count_id', 'product_id', name='uq_stock_count_line'),
    )
    op.create_index('ix_stock_count_lines_count', 'stock_count_lines', ['count_id'])


def downgrade():
    op.drop_index('ix_stock_count_lines_count', table_name='stock_count_lines')
    op.drop_table('stock_count_lines')
    op.drop_index('ix_stock_counts_org_status', table_name='stock_counts')
    op.drop_index('ix_stock_counts_one_active', table_name='stock_counts')
    op.drop_table('stock_counts')
