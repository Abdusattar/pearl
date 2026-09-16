"""Бот: журнал сообщений и Telegram-id людей как BigInteger

Revision ID: s0916a0005
Revises: s0916a0004
Create Date: 2026-09-16

Макет блок 7 (решение владельца 15.09): в группу только сигналы, в личку —
карман держателю по пятницам и сводка учредителям через проверку Абдусаттара.
`bot_messages` — что и когда отправлено или получено: расписание не шлёт
одно и то же дважды (job + date уникальны), сводка учредителям ждёт «ок»
владельца, ответы людей привязаны к вопросу. `users.tg_id` был Integer —
Telegram id давно больше 2^31.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 's0916a0005'
down_revision = 's0916a0004'
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column('users', 'tg_id', type_=sa.BigInteger(), existing_type=sa.Integer())
    op.create_table(
        'bot_messages',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('kind', sa.String(40), nullable=False),      # group_signals | pocket_ask | founders_review | founders_summary | inbound | reply
        sa.Column('job_key', sa.String(60), nullable=True),    # kind:date[:user] — защита от повтора по расписанию
        sa.Column('chat_id', sa.BigInteger(), nullable=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('direction', sa.String(3), nullable=False, server_default='out'),  # out | in
        sa.Column('text', sa.Text(), nullable=True),
        sa.Column('status', sa.String(20), nullable=False, server_default='sent'),  # sent | pending | approved | rejected | answered | failed
        sa.Column('payload', postgresql.JSONB(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint('job_key', name='uq_bot_messages_job_key'),
    )
    op.create_index('ix_bot_messages_kind_created', 'bot_messages', ['kind', 'created_at'])


def downgrade():
    op.drop_index('ix_bot_messages_kind_created', table_name='bot_messages')
    op.drop_table('bot_messages')
    op.alter_column('users', 'tg_id', type_=sa.Integer(), existing_type=sa.BigInteger())
