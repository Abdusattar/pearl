"""add recurring_expense_templates + transactions.recurring_template_id

Revision ID: h8i9j0k1l2m3
Revises: g7h8i9j0k1l2
Create Date: 2026-07-12
"""
import sqlalchemy as sa
from alembic import op

revision = 'h8i9j0k1l2m3'
down_revision = 'g7h8i9j0k1l2'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'recurring_expense_templates',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('organization_id', sa.Integer, sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('name', sa.String(150), nullable=False),
        sa.Column('category_id', sa.Integer, sa.ForeignKey('expense_categories.id'), nullable=False),
        sa.Column('amount_source', sa.String(20), nullable=False, server_default='manual'),
        sa.Column('owner_only', sa.Boolean, nullable=False, server_default='false'),
        sa.Column('active', sa.Boolean, nullable=False, server_default='true'),
        sa.Column('created_by', sa.Integer, sa.ForeignKey('users.id')),
        sa.Column('created_at', sa.DateTime, server_default=sa.func.now()),
    )
    op.add_column(
        'transactions',
        sa.Column('recurring_template_id', sa.Integer,
                  sa.ForeignKey('recurring_expense_templates.id'), nullable=True),
    )


def downgrade():
    op.drop_column('transactions', 'recurring_template_id')
    op.drop_table('recurring_expense_templates')
