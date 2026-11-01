"""Optional team deadlines and a durable stop/resume snapshot."""
import sqlalchemy as sa
from alembic import op

revision = '0080'
down_revision = '0079'
branch_labels = depends_on = None


def upgrade():
    with op.batch_alter_table('team') as batch:
        batch.alter_column('expires_at', existing_type=sa.DateTime(), nullable=True)
        batch.add_column(sa.Column('resume_members', sa.JSON(), nullable=False, server_default='[]'))
        batch.add_column(sa.Column('lifetime_revision', sa.Integer(), nullable=False, server_default='0'))
        batch.add_column(sa.Column('stop_reason', sa.String(20), nullable=True))


def downgrade():
    if op.get_bind().scalar(sa.text('SELECT count(*) FROM team WHERE expires_at IS NULL')):
        raise RuntimeError('Unlimited teams cannot be downgraded. Restore the pre-upgrade backup or assign explicit deadlines first.')
    with op.batch_alter_table('team') as batch:
        batch.drop_column('stop_reason')
        batch.drop_column('lifetime_revision')
        batch.drop_column('resume_members')
        batch.alter_column('expires_at', existing_type=sa.DateTime(), nullable=False)
