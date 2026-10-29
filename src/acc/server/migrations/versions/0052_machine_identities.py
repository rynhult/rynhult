"""Machine identities in the device registry: the enrolled node a leaf belongs to, and rotation overlap."""
import sqlalchemy as sa
from alembic import op

revision = '0052'
down_revision = '0051'
branch_labels = depends_on = None


def upgrade():
    with op.batch_alter_table('device') as batch:
        batch.add_column(sa.Column('node_id', sa.String(32)))
        batch.add_column(sa.Column('retire_after', sa.DateTime()))
        batch.create_index('ix_device_node_id', ['node_id'])


def downgrade():
    with op.batch_alter_table('device') as batch:
        batch.drop_index('ix_device_node_id')
        batch.drop_column('retire_after')
        batch.drop_column('node_id')
