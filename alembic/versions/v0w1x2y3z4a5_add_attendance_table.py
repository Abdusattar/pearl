"""add attendance table — утренняя отметка посещаемости по группам,
чтобы повар готовил на факт явки, а не на списочный состав

Revision ID: v0w1x2y3z4a5
Revises: u9v0w1x2y3z4
Create Date: 2026-07-09
"""
from alembic import op
import sqlalchemy as sa

revision = 'v0w1x2y3z4a5'
down_revision = 'u9v0w1x2y3z4'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'attendance',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('student_id', sa.Integer(), sa.ForeignKey('students.id'), nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('present', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('organization_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint('student_id', 'date', name='uq_attendance_student_date'),
    )
    op.create_index('ix_attendance_date', 'attendance', ['date'])


def downgrade():
    op.drop_index('ix_attendance_date', table_name='attendance')
    op.drop_table('attendance')
