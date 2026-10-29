"""session changes: the workspace's git HEAD at launch, and a summary of what changed since

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-21 23:00:00
"""
import sqlalchemy as sa
from alembic import op

import acc.server.models

revision = '0015'
down_revision = '0014'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('session') as batch:
        batch.add_column(sa.Column('base_commit', sa.String(length=40), nullable=True))
        batch.add_column(sa.Column('changes', sa.JSON(), nullable=True))
        batch.add_column(sa.Column('changes_at', acc.server.models.UTCDateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('session') as batch:
        batch.drop_column('changes_at')
        batch.drop_column('changes')
        batch.drop_column('base_commit')
