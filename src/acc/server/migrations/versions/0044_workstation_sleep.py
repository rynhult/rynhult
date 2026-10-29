"""Opt-in workstation sleep and durable session idle/pause markers."""
import sqlalchemy as sa
from alembic import op

from acc.server.models import UTCDateTime

revision = '0044'
down_revision = '0043'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('node', sa.Column('idle_sleep_minutes', sa.Integer(), nullable=True))
    op.add_column('session', sa.Column('idle_since', UTCDateTime(), nullable=True))
    op.add_column('session', sa.Column('sleep_paused_at', UTCDateTime(), nullable=True))


def downgrade():
    op.drop_column('session', 'sleep_paused_at')
    op.drop_column('session', 'idle_since')
    op.drop_column('node', 'idle_sleep_minutes')
