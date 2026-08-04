"""charges — уникальность (student_id, date) для ежемесячного начисления

Защита на уровне схемы от повтора бага 04.08 (гонка в generate_monthly_charges
задвоила начисления 50 детям на 1.6млн сом, см. app/services/billing.py —
advisory lock уже закрывает конкретную гонку, это второй слой). Частичный
индекс — не общий unique(student_id, date, description) — потому что Charge
используется и для ручных корректировок, которых в один день может быть
несколько.

Revision ID: v1w2x3y4z5a6
Revises: t9u0v1w2x3y4
Create Date: 2026-08-04
"""
from alembic import op
import sqlalchemy as sa

revision = 'v1w2x3y4z5a6'
down_revision = 't9u0v1w2x3y4'
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        'uq_charges_student_month',
        'charges',
        ['student_id', 'date'],
        unique=True,
        postgresql_where=sa.text("description = 'Начисление за месяц'"),
    )


def downgrade():
    op.drop_index('uq_charges_student_month', table_name='charges')
