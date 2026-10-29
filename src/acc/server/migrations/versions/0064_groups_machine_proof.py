"""Require local account-control proof before approving new machine bindings."""
import sqlalchemy as sa
from alembic import op

revision = '0064'
down_revision = '0063'
branch_labels = depends_on = None


def upgrade():
    with op.batch_alter_table('group_machine_account') as batch:
        batch.add_column(sa.Column('proof_verified_at', sa.DateTime(timezone=True)))


def downgrade():
    with op.batch_alter_table('group_machine_account') as batch:
        batch.drop_column('proof_verified_at')
