"""«Обучение» — реальная услуга с редактируемой ценой, не константа в коде
(проговорено с Абдусаттаром 13.07: тариф будет расти, менять его нужно
обычной правкой на /services/, не деплоем). Добавлен services.is_tuition —
один такой на объект, применяется автоматически всем активным детям, не
через чекбокс StudentService как обычные услуги. Сидируется для Садика
Сокулук (org_id по имени организации) ценой 8000 — совпадает с базой,
на которую уже мигрировали discount_amount в предыдущей миграции.

Revision ID: q7r8s9t0u1v2
Revises: p6q7r8s9t0u1
Create Date: 2026-07-13
"""
import sqlalchemy as sa
from alembic import op

revision = 'q7r8s9t0u1v2'
down_revision = 'p6q7r8s9t0u1'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()

    op.add_column('services', sa.Column('is_tuition', sa.Boolean(), nullable=False, server_default='false'))

    conn.execute(sa.text(
        "INSERT INTO services (organization_id, name, price, is_tuition) "
        "SELECT o.id, 'Обучение', 8000, true "
        "FROM organizations o WHERE o.name = 'Садик Сокулук' "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM services s WHERE s.organization_id = o.id AND s.is_tuition = true"
        ")"
    ))


def downgrade():
    # необратимо разводить обратно вручную — намеренно без авто-downgrade
    pass
