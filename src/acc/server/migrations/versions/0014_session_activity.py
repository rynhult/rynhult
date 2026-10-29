"""session activity: what the agent is doing now, from its tool hooks

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-21 22:00:00
"""
import sqlalchemy as sa
from alembic import op

import acc.server.models

revision = '0014'
down_revision = '0013'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('session') as batch:
        batch.add_column(sa.Column('activity', sa.String(length=200), nullable=True))
        batch.add_column(sa.Column('activity_at', acc.server.models.UTCDateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('session') as batch:
        batch.drop_column('activity_at')
        batch.drop_column('activity')
