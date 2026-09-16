"""Лист кухни: один лист на день, строки — обычные списания

Revision ID: s0916a0002
Revises: s0916a0001
Create Date: 2026-09-16

Макет версии 7, блок 3б; план `context/revision/08_kitchen_sheet.md`.
Повара пишут на бумаге, что взяли за день (один лист на день, без деления
по приёмам пищи), Махабат переносит в систему. Шапка листа нужна, чтобы
«день внесён / не внесён» было фактом, а не догадкой по наличию списаний:
пересчёт склада и слияние площадок тоже пишут списания. В шапке едоков,
фото листа и расхождения (взяли больше, чем числилось: списано что было,
разница записана, а не спрятана в минусе остатка).

`write_offs.sheet_id` — строка листа. Старый склад видит такие списания как
раньше (reason «лист кухни»).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 's0916a0002'
down_revision = 's0916a0001'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'kitchen_sheets',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('site_org_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('children_count', sa.Integer(), nullable=True),
        sa.Column('photo_path', sa.String(500), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        # [{product_id, name, taken, had, unit}] — взяли больше, чем числилось
        sa.Column('shortfalls', postgresql.JSONB(), nullable=True),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('site_org_id', 'date', name='uq_kitchen_sheet_site_date'),
    )
    op.add_column('write_offs', sa.Column('sheet_id', sa.Integer(),
                                          sa.ForeignKey('kitchen_sheets.id'), nullable=True))
    op.create_index('ix_write_offs_sheet', 'write_offs', ['sheet_id'])


def downgrade():
    op.drop_index('ix_write_offs_sheet', table_name='write_offs')
    op.drop_column('write_offs', 'sheet_id')
    op.drop_table('kitchen_sheets')
