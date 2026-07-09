"""add assets table — лёгкий реестр капитальных покупок (мебель/оборудование),
база для будущей амортизации у бухгалтера, без расчёта амортизации в Pearl

Revision ID: t8u9v0w1x2y3
Revises: s7t8u9v0w1x2
Create Date: 2026-07-09
"""
from alembic import op
import sqlalchemy as sa

revision = 't8u9v0w1x2y3'
down_revision = 's7t8u9v0w1x2'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'assets',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('category', sa.String(length=50), nullable=True),
        sa.Column('purchase_date', sa.Date(), nullable=False),
        sa.Column('cost', sa.Numeric(12, 2), nullable=False),
        sa.Column('organization_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
    )


def downgrade():
    op.drop_table('assets')
