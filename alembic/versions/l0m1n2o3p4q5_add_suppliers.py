"""add suppliers table and supplier_id to transactions

Revision ID: l0m1n2o3p4q5
Revises: k9l0m1n2o3p4
Create Date: 2026-06-29
"""
from alembic import op
import sqlalchemy as sa

revision = 'l0m1n2o3p4q5'
down_revision = 'k9l0m1n2o3p4'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'suppliers',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('name', sa.String(200), nullable=False),
        sa.Column('phone', sa.String(20), unique=True, nullable=True),
        sa.Column('created_at', sa.DateTime, server_default=sa.func.now()),
    )

    op.execute("INSERT INTO suppliers (name) VALUES ('Рынок')")
    op.execute("INSERT INTO suppliers (name) VALUES ('Магазин Народный')")
    op.execute("INSERT INTO suppliers (name) VALUES ('Магазин')")

    op.add_column('transactions',
        sa.Column('supplier_id', sa.Integer, sa.ForeignKey('suppliers.id'), nullable=True)
    )


def downgrade():
    op.drop_column('transactions', 'supplier_id')
    op.drop_table('suppliers')
