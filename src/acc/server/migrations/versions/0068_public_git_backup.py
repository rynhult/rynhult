"""Owner opt-in per machine for public-address git backup transport (2026-10-25)."""
import sqlalchemy as sa
from alembic import op

revision = '0068'
down_revision = '0067'  # coordinator: after mobile (0067)
branch_labels = depends_on = None


def upgrade():
    with op.batch_alter_table('node') as batch:
        batch.add_column(sa.Column('public_git_backup', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    with op.batch_alter_table('node') as batch:
        batch.drop_column('public_git_backup')
