"""add mylomojka category

Revision ID: k9l0m1n2o3p4
Revises: j8k9l0m1n2o3
Create Date: 2026-06-29
"""
from alembic import op

revision = 'k9l0m1n2o3p4'
down_revision = 'j8k9l0m1n2o3'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "INSERT INTO expense_categories (name, parent_id, organization_id) "
        "VALUES ('Мыломойка', NULL, NULL) "
        "ON CONFLICT DO NOTHING"
    )


def downgrade():
    op.execute(
        "DELETE FROM expense_categories WHERE name = 'Мыломойка' AND parent_id IS NULL"
    )
