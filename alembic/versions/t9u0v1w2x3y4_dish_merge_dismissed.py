"""dish_merge_dismissed — пары блюд, помеченные "это разные блюда, не предлагать" на экране слияния дублей

Revision ID: t9u0v1w2x3y4
Revises: s8t9u0v1w2x3
Create Date: 2026-07-29
"""
from alembic import op
import sqlalchemy as sa

revision = 't9u0v1w2x3y4'
down_revision = 's8t9u0v1w2x3'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'dish_merge_dismissed',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('dish_id_a', sa.Integer(), sa.ForeignKey('dishes.id'), nullable=False),
        sa.Column('dish_id_b', sa.Integer(), sa.ForeignKey('dishes.id'), nullable=False),
        sa.Column('dismissed_at', sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_unique_constraint(
        'uq_dish_merge_dismissed_pair', 'dish_merge_dismissed', ['dish_id_a', 'dish_id_b']
    )


def downgrade():
    op.drop_table('dish_merge_dismissed')
