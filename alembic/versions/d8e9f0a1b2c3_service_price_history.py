"""service_price_history — журнал изменений цены услуги

Revision ID: d8e9f0a1b2c3
Revises: c7d8e9f0a1b2
Create Date: 2026-07-22
"""
from alembic import op
import sqlalchemy as sa

revision = 'd8e9f0a1b2c3'
down_revision = 'c7d8e9f0a1b2'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'service_price_history',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('service_id', sa.Integer(), sa.ForeignKey('services.id'), nullable=False),
        sa.Column('price', sa.Numeric(10, 2), nullable=False),
        sa.Column('effective_date', sa.Date(), nullable=False),
        sa.Column('changed_by', sa.Integer(), sa.ForeignKey('users.id')),
        sa.Column('changed_at', sa.DateTime(), server_default=sa.func.now()),
    )
    # Бэкфилл: у каждой существующей услуги уже есть цена — заводим для неё
    # одну историческую запись "как есть сейчас", чтобы текущая цена не
    # потерялась как только кто-то в первый раз её поправит.
    op.execute("""
        INSERT INTO service_price_history (service_id, price, effective_date, changed_by)
        SELECT id, price, COALESCE(created_at::date, CURRENT_DATE), NULL
        FROM services
    """)


def downgrade():
    op.drop_table('service_price_history')
