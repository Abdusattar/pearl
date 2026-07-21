"""attendance.reason / attendance.comment — причина отсутствия

Revision ID: c7d8e9f0a1b2
Revises: b6c7d8e9f0a1
Create Date: 2026-07-21
"""
from alembic import op
import sqlalchemy as sa

revision = 'c7d8e9f0a1b2'
down_revision = 'b6c7d8e9f0a1'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('attendance', sa.Column('reason', sa.String(20), nullable=True))
    op.add_column('attendance', sa.Column('comment', sa.Text(), nullable=True))


def downgrade():
    op.drop_column('attendance', 'comment')
    op.drop_column('attendance', 'reason')
