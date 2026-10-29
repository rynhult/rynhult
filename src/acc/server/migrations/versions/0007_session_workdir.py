"""session workdir (standalone research sessions run in their own folder)

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-18 14:00:00
"""
import sqlalchemy as sa
from alembic import op

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('session', schema=None) as batch_op:
        batch_op.add_column(sa.Column('workdir', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('session', schema=None) as batch_op:
        batch_op.drop_column('workdir')
