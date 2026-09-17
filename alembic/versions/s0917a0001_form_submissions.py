"""Одна отправка формы — одна запись

Revision ID: s0917a0001
Revises: s0916a0005
Create Date: 2026-09-17

Владелец 17.09: «она может два раза нажимать из-за плохого интернета — учесть
везде». Номер формы запоминается вместе с записью; повтор того же номера
ведёт на уже созданное, а не пишет второй раз.
"""
from alembic import op
import sqlalchemy as sa

revision = 's0917a0001'
down_revision = 's0916a0005'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'form_submissions',
        sa.Column('token', sa.String(64), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('result_url', sa.String(300), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
    )


def downgrade():
    op.drop_table('form_submissions')
