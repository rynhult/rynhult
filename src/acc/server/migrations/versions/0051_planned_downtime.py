"""Owner downtime policy and durable reconnect identity."""
import sqlalchemy as sa
from alembic import op

revision = '0051'
down_revision = '0050'
branch_labels = depends_on = None


def upgrade():
    op.add_column('node', sa.Column('downtime_policy', sa.JSON(), nullable=True))
    op.add_column('node', sa.Column('downtime_until_reconnect', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('node', sa.Column('last_reconnected_at', sa.DateTime(), nullable=True))


def downgrade():
    for name in ('last_reconnected_at', 'downtime_until_reconnect', 'downtime_policy'):
        op.drop_column('node', name)
