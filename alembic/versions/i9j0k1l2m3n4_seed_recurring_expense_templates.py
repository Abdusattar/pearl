"""seed recurring expense templates for Sadik Sokulup (pilot org)

Revision ID: i9j0k1l2m3n4
Revises: h8i9j0k1l2m3
Create Date: 2026-07-12
"""
import sqlalchemy as sa
from alembic import op

revision = 'i9j0k1l2m3n4'
down_revision = 'h8i9j0k1l2m3'
branch_labels = None
depends_on = None

# (имя шаблона, имя категории, amount_source, owner_only)
TEMPLATES = [
    ("ФОТ (зарплата)", "Зарплата", "employees_sum", True),
    ("Охрана", "Охрана", "last_amount", False),
    ("Электричество", "Электричество", "manual", False),
    ("Вода", "Вода", "manual", False),
    ("Отопление", "Отопление", "manual", False),
    ("Интернет", "Интернет", "manual", False),
]

_INSERT = sa.text(
    "INSERT INTO recurring_expense_templates "
    "(organization_id, name, category_id, amount_source, owner_only, active) "
    "SELECT o.id, :name, c.id, :amount_source, :owner_only, true "
    "FROM organizations o, expense_categories c "
    "WHERE o.name = 'Садик Сокулук' AND c.name = :cat_name "
    "AND NOT EXISTS ("
    "  SELECT 1 FROM recurring_expense_templates t "
    "  WHERE t.organization_id = o.id AND t.name = :name"
    ")"
)

_DELETE = sa.text("DELETE FROM recurring_expense_templates WHERE name = :name")


def upgrade():
    conn = op.get_bind()
    for name, cat_name, amount_source, owner_only in TEMPLATES:
        conn.execute(_INSERT, {
            "name": name, "cat_name": cat_name,
            "amount_source": amount_source, "owner_only": owner_only,
        })


def downgrade():
    conn = op.get_bind()
    for name, _, _, _ in TEMPLATES:
        conn.execute(_DELETE, {"name": name})
