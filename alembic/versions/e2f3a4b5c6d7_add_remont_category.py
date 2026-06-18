"""add remont expense category

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Branch Labels: None
Depends On: None
"""
from alembic import op
import sqlalchemy as sa

revision = 'e2f3a4b5c6d7'
down_revision = 'd1e2f3a4b5c6'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "SELECT setval('expense_categories_id_seq', "
        "(SELECT MAX(id) FROM expense_categories))"
    )
    op.execute(
        "INSERT INTO expense_categories (name, parent_id, organization_id) "
        "VALUES ('Ремонт', NULL, NULL)"
    )


def downgrade():
    op.execute(
        "DELETE FROM expense_categories WHERE name = 'Ремонт' AND parent_id IS NULL"
    )
