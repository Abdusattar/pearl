"""add dishes, menu_entries tables + write_offs.dish_id — Меню/приёмы пищи,
блюпринт wiki/blueprints/menu_module.md

Revision ID: d4e5f6g7h8i9
Revises: x2y3z4a5b6c7
Create Date: 2026-07-10
"""
from alembic import op
import sqlalchemy as sa

revision = 'd4e5f6g7h8i9'
down_revision = 'x2y3z4a5b6c7'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'dishes',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('name', sa.String(150), nullable=False, unique=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_table(
        'menu_entries',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('organization_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('meal_type', sa.String(20), nullable=False),
        sa.Column('dish_id', sa.Integer(), sa.ForeignKey('dishes.id'), nullable=False),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index('ix_menu_entries_org_date', 'menu_entries', ['organization_id', 'date'])
    op.add_column('write_offs', sa.Column('dish_id', sa.Integer(), sa.ForeignKey('dishes.id'), nullable=True))


def downgrade():
    op.drop_column('write_offs', 'dish_id')
    op.drop_index('ix_menu_entries_org_date', table_name='menu_entries')
    op.drop_table('menu_entries')
    op.drop_table('dishes')
