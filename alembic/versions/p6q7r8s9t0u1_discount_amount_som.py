"""скидка на тариф — в сомах, не в процентах (проговорено с Абдусаттаром 13.07)

Базовый тариф стал константой (DEFAULT_MONTHLY_FEE=8000 в services/billing.py),
Student.discount_percent (никогда не использовался, 0 у всех) переименован в
discount_amount и расширен под сумму в сомах. Существующие Student.extra.monthly_fee
мигрируются в discount_amount = 8000 - monthly_fee, чтобы у уже посчитанных
детей тариф не скакнул при следующем начислении. Дети без monthly_fee получат
discount_amount=0 → базовый тариф 8000 (раньше им начисление не создавалось
вообще — 0 <= 0 пропускался в generate_monthly_charges).

Revision ID: p6q7r8s9t0u1
Revises: o5p6q7r8s9t0
Create Date: 2026-07-13
"""
import sqlalchemy as sa
from alembic import op

revision = 'p6q7r8s9t0u1'
down_revision = 'o5p6q7r8s9t0'
branch_labels = None
depends_on = None

DEFAULT_FEE = 8000


def upgrade():
    conn = op.get_bind()

    op.alter_column(
        'students', 'discount_percent',
        new_column_name='discount_amount',
        type_=sa.Numeric(10, 2),
        existing_type=sa.Numeric(5, 2),
    )

    # перенести существующий monthly_fee (JSON extra) в discount_amount —
    # сохраняет фактический тариф каждого ребёнка неизменным
    rows = conn.execute(sa.text(
        "SELECT id, extra FROM students WHERE extra ->> 'monthly_fee' IS NOT NULL"
    )).fetchall()
    for sid, extra in rows:
        fee = extra.get('monthly_fee') if extra else None
        if fee is None:
            continue
        discount = max(0, DEFAULT_FEE - float(fee))
        conn.execute(sa.text(
            "UPDATE students SET discount_amount = :d WHERE id = :id"
        ), {"d": discount, "id": sid})


def downgrade():
    # необратимо разводить обратно вручную — намеренно без авто-downgrade
    pass
