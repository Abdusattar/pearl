"""add discount fields to students

Revision ID: j0k1l2m3n4o5
Revises: i9j0k1l2m3n4
Create Date: 2026-07-12
"""
import sqlalchemy as sa
from alembic import op

revision = 'j0k1l2m3n4o5'
down_revision = 'i9j0k1l2m3n4'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('students', sa.Column('discount_percent', sa.Numeric(5, 2), nullable=False, server_default='0'))
    op.add_column('students', sa.Column('discount_reason', sa.Text, nullable=True))
    op.add_column('students', sa.Column('discount_set_by', sa.Integer, sa.ForeignKey('users.id'), nullable=True))
    op.add_column('students', sa.Column('discount_set_at', sa.DateTime, nullable=True))


def downgrade():
    op.drop_column('students', 'discount_set_at')
    op.drop_column('students', 'discount_set_by')
    op.drop_column('students', 'discount_reason')
    op.drop_column('students', 'discount_percent')
