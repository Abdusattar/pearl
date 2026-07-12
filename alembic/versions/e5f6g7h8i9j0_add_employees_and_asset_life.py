"""add employees table + assets.useful_life_months — ФОТ/Амортизация
считаются на лету, не хранятся как Transaction, см.
wiki/blueprints/unit_economics_module.md

Revision ID: e5f6g7h8i9j0
Revises: d4e5f6g7h8i9
Create Date: 2026-07-10
"""
from alembic import op
import sqlalchemy as sa

revision = 'e5f6g7h8i9j0'
down_revision = 'd4e5f6g7h8i9'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'employees',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('organization_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('full_name', sa.String(150), nullable=False),
        sa.Column('role', sa.String(100), nullable=True),
        sa.Column('salary', sa.Numeric(12, 2), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='active'),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
    )
    op.add_column('assets', sa.Column('useful_life_months', sa.Integer(), nullable=True))


def downgrade():
    op.drop_column('assets', 'useful_life_months')
    op.drop_table('employees')
