"""Единая таблица сверок: счёт, касса, долг поставщику — с сохранением ожидаемой суммы

Revision ID: f1a2b3c4d5e6
Revises: e1f2a3b4c5d6
Create Date: 2026-09-07

Причина (07.09): account_balance_snapshots хранил только заявленный остаток.
Расхождение с расчётом нигде не оставалось, новая сверка становилась новой
базой — недостача исчезала бесследно. В этот же день на Садике так и вышло:
снятые 60 000 попали в поле остатка, счёт разъехался, а восстанавливать
ожидаемую цифру (198 779) пришлось скриптом по базе.

Старую таблицу не удаляем — данные переносим, но оставляем на месте, пока
новый механизм не отработает на проде.
"""
from alembic import op
import sqlalchemy as sa

revision = 'f1a2b3c4d5e6'
down_revision = 'e1f2a3b4c5d6'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'reconciliations',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('organization_id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=20), nullable=False),
        sa.Column('subject_id', sa.Integer(), nullable=True),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('expected_amount', sa.Numeric(12, 2), nullable=False),
        sa.Column('actual_amount', sa.Numeric(12, 2), nullable=False),
        sa.Column('delta', sa.Numeric(12, 2), nullable=False),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.Column('cancelled_at', sa.DateTime(), nullable=True),
        sa.Column('cancelled_by', sa.Integer(), nullable=True),
        sa.Column('cancel_reason', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id']),
        sa.ForeignKeyConstraint(['created_by'], ['users.id']),
        sa.ForeignKeyConstraint(['cancelled_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    # Выборка всегда идёт «последняя не отменённая сверка этого вида до даты X».
    op.create_index('ix_reconciliations_lookup', 'reconciliations',
                    ['organization_id', 'kind', 'date'])

    # Перенос истории сверок счёта. expected для старых записей неизвестен —
    # его никогда не сохраняли; ставим равным факту, чтобы delta вышла 0 и
    # старые строки не выглядели расхождениями, которых никто не видел.
    op.execute("""
        INSERT INTO reconciliations
            (organization_id, kind, date, expected_amount, actual_amount, delta,
             reason, created_by, created_at)
        SELECT organization_id, 'account', date, balance, balance, 0,
               comment, created_by, created_at
        FROM account_balance_snapshots
        ORDER BY id
    """)


def downgrade():
    op.drop_index('ix_reconciliations_lookup', table_name='reconciliations')
    op.drop_table('reconciliations')
