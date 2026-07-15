"""suppliers.phone becomes required, with '0000' placeholder exempt from uniqueness

Revision ID: z4a5b6c7d8e9
Revises: y3z4a5b6c7d8
Create Date: 2026-07-15
"""
from alembic import op
import sqlalchemy as sa

revision = 'z4a5b6c7d8e9'
down_revision = 'y3z4a5b6c7d8'
branch_labels = None
depends_on = None

PLACEHOLDER = '0000'


def upgrade():
    # Снять старый constraint ДО backfill — иначе UPDATE, проставляющий один и тот же
    # плейсхолдер нескольким строкам без телефона разом, падает на ещё живом
    # suppliers_phone_key (поймано локальным тестом: на dev было >1 такой строки).
    op.drop_constraint('suppliers_phone_key', 'suppliers', type_='unique')
    op.execute(f"UPDATE suppliers SET phone = '{PLACEHOLDER}' WHERE phone IS NULL")
    op.alter_column('suppliers', 'phone', existing_type=sa.String(20), nullable=False)
    op.create_index(
        'ix_suppliers_phone_unique', 'suppliers', ['phone'],
        unique=True, postgresql_where=sa.text(f"phone != '{PLACEHOLDER}'"),
    )


def downgrade():
    op.drop_index('ix_suppliers_phone_unique', table_name='suppliers')
    op.execute(f"UPDATE suppliers SET phone = NULL WHERE phone = '{PLACEHOLDER}'")
    op.alter_column('suppliers', 'phone', existing_type=sa.String(20), nullable=True)
    op.create_unique_constraint('suppliers_phone_key', 'suppliers', ['phone'])
