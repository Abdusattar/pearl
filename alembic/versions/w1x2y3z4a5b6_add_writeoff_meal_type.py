"""add meal_type to write_offs — привязка списания к конкретному приёму пищи
(завтрак/обед/полдник/ужин), закрывает вопрос "за какое блюдо списание"

Revision ID: w1x2y3z4a5b6
Revises: v0w1x2y3z4a5
Create Date: 2026-07-09
"""
from alembic import op
import sqlalchemy as sa

revision = 'w1x2y3z4a5b6'
down_revision = 'v0w1x2y3z4a5'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('write_offs', sa.Column('meal_type', sa.String(length=20), nullable=True))


def downgrade():
    op.drop_column('write_offs', 'meal_type')
