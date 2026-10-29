"""Per-session containers (docs/CONTAINER-LAUNCHER.md): a project's switch, and which sessions run in a container."""
import sqlalchemy as sa
from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = depends_on = None


def upgrade():
    with op.batch_alter_table('project') as batch:
        batch.add_column(sa.Column('containers', sa.Boolean(), nullable=False, server_default=sa.false()))
    with op.batch_alter_table('session') as batch:
        batch.add_column(sa.Column('container', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column('container_generation', sa.Integer()))


def downgrade():
    with op.batch_alter_table('session') as batch:
        batch.drop_column('container_generation')
        batch.drop_column('container')
    with op.batch_alter_table('project') as batch:
        batch.drop_column('containers')
