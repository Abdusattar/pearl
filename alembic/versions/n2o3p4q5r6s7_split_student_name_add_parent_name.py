"""split student.name into last_name/first_name/patronymic, add parent_name

Revision ID: n2o3p4q5r6s7
Revises: m1n2o3p4q5r6
Create Date: 2026-07-04
"""
from alembic import op
import sqlalchemy as sa

revision = 'n2o3p4q5r6s7'
down_revision = 'm1n2o3p4q5r6'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('students', sa.Column('last_name', sa.String(50)))
    op.add_column('students', sa.Column('first_name', sa.String(50)))
    op.add_column('students', sa.Column('patronymic', sa.String(50)))
    op.add_column('students', sa.Column('parent_name', sa.String(100)))

    # Бэкфилл: существующее "name" всегда было "Фамилия Имя[ Отчество]"
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, name FROM students")).fetchall()
    for student_id, name in rows:
        parts = (name or "").split()
        last_name = parts[0] if parts else ""
        first_name = parts[1] if len(parts) > 1 else ""
        patronymic = " ".join(parts[2:]) if len(parts) > 2 else None
        conn.execute(
            sa.text(
                "UPDATE students SET last_name=:l, first_name=:f, patronymic=:p WHERE id=:id"
            ),
            {"l": last_name, "f": first_name, "p": patronymic, "id": student_id},
        )


def downgrade():
    op.drop_column('students', 'parent_name')
    op.drop_column('students', 'patronymic')
    op.drop_column('students', 'first_name')
    op.drop_column('students', 'last_name')
