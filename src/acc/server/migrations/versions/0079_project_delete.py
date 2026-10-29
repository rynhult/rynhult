"""Checkout provenance and detached session history."""
import sqlalchemy as sa
from alembic import op

revision = '0079'
down_revision = '0078'
branch_labels = depends_on = None


def upgrade():
    # Older registrations have no trustworthy creation provenance; keep their folders.
    column = sa.Column('managed_checkout', sa.Boolean(), nullable=False, server_default='0')
    if op.get_bind().dialect.name == 'sqlite':
        # Retained audit, backup and alert identities must never name a new project.
        with op.batch_alter_table('project', recreate='always', table_kwargs={'sqlite_autoincrement': True}) as batch:
            batch.add_column(column)
    else:
        op.add_column('project', column)
    op.add_column('project', sa.Column('deleting', sa.Boolean(), nullable=False, server_default='0'))
    op.add_column('session', sa.Column('deleted_project', sa.JSON(), nullable=True))
    for name in ('session', 'multirun'):
        with op.batch_alter_table(name) as batch:
            batch.alter_column('project_id', existing_type=sa.Integer(), nullable=True)


def downgrade():
    # History cannot be reattached to a removed project without fabricating it.
    connection = op.get_bind()
    if connection.scalar(sa.text('SELECT count(*) FROM session WHERE project_id IS NULL')) or connection.scalar(
            sa.text('SELECT count(*) FROM multirun WHERE project_id IS NULL')):
        raise RuntimeError('Detached project history exists; restore a pre-deletion backup before downgrading.')
    for name in ('session', 'multirun'):
        with op.batch_alter_table(name) as batch:
            batch.alter_column('project_id', existing_type=sa.Integer(), nullable=False)
    op.drop_column('session', 'deleted_project')
    op.drop_column('project', 'managed_checkout')
    op.drop_column('project', 'deleting')
