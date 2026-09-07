"""transactions.employee_id — выдача зарплаты по каждому сотруднику

Revision ID: a2b3c4d5e6f7
Revises: f1a2b3c4d5e6
Create Date: 2026-09-07

До этого зарплата проводилась одной суммой на всех (RecurringExpenseTemplate
«ФОТ (зарплата)», подсказка = SUM окладов активных). По такой проводке нельзя
сказать, кто сколько получил и кому ещё должны — а выдают по факту: аванс,
часть, неполный месяц. Заказчик 07.09: «желательно провести по каждому, потому
что сумма может быть меньше».
"""
from alembic import op
import sqlalchemy as sa

revision = 'a2b3c4d5e6f7'
down_revision = 'f1a2b3c4d5e6'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('transactions', sa.Column('employee_id', sa.Integer(), nullable=True))
    op.create_foreign_key(
        'fk_transactions_employee', 'transactions', 'employees', ['employee_id'], ['id']
    )
    # Выборка всегда «выдачи этого сотрудника за месяц» — по employee_id + period.
    op.create_index('ix_transactions_employee_period', 'transactions', ['employee_id', 'period'])


def downgrade():
    op.drop_index('ix_transactions_employee_period', table_name='transactions')
    op.drop_constraint('fk_transactions_employee', 'transactions', type_='foreignkey')
    op.drop_column('transactions', 'employee_id')
