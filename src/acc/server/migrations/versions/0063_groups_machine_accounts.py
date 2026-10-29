"""Explicit own-machine group account authority; no existing placement gains access."""
import sqlalchemy as sa
from alembic import op

revision = '0063'
down_revision = '0054'
branch_labels = depends_on = None


def upgrade():
    # Freeze the historical schema: later model fields belong to later revisions.
    op.create_table('group_machine_account',
        sa.Column('id', sa.String(32), primary_key=True),
        sa.Column('node_id', sa.String(32), sa.ForeignKey('node.id'), nullable=False),
        sa.Column('person', sa.String(64), nullable=False),
        sa.Column('account', sa.String(32), nullable=False),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('approved_by', sa.String(64)))
    op.create_index('uq_group_machine_account_reserved', 'group_machine_account', ['node_id', 'account'],
                    unique=True, sqlite_where=sa.text('approved_by IS NOT NULL'),
                    postgresql_where=sa.text('approved_by IS NOT NULL'))
    with op.batch_alter_table('placement') as batch:
        batch.add_column(sa.Column('machine_account_id', sa.String(32)))
        batch.create_foreign_key('fk_placement_machine_account', 'group_machine_account', ['machine_account_id'], ['id'])


def downgrade():
    # Approved (and later revoked) bindings keep a removed person's agent account from going to someone else; the
    # table can't come back with them, so refuse rather than lose them (security review 8, SR8-8). Pending requests
    # reserve nothing.
    reserved = op.get_bind().execute(sa.text(
        'SELECT count(*) FROM group_machine_account WHERE approved_by IS NOT NULL')).scalar()
    if reserved:
        raise RuntimeError(f'{reserved} group machine account binding(s) reserve agent accounts; downgrading below 0063 '
                           'would lose them for good. Keep this version, or delete those rows deliberately first.')
    with op.batch_alter_table('placement') as batch:
        batch.drop_constraint('fk_placement_machine_account', type_='foreignkey')
        batch.drop_column('machine_account_id')
    op.drop_table('group_machine_account')
