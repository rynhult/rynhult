"""compose pin: the compose files an owner approved for autostart (D4)

Revision ID: 0027
Revises: 0026
Create Date: 2026-10-22 20:00:00
"""
import sqlalchemy as sa
from alembic import op

revision = '0027'
down_revision = '0026'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('project') as batch_op:
        batch_op.add_column(sa.Column('compose_pin', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('project') as batch_op:
        batch_op.drop_column('compose_pin')
