"""Scoped security policies, explicit defaults and browser-bound changes."""
import sqlalchemy as sa
from alembic import op

revision = '0081'
down_revision = '0080'
branch_labels = depends_on = None


def upgrade():
    op.create_table('security_policy', sa.Column('key', sa.String(200), primary_key=True),
                    sa.Column('scope', sa.JSON(), nullable=False), sa.Column('overrides', sa.JSON(), nullable=False),
                    sa.Column('registry_version', sa.Integer(), nullable=False), sa.Column('previous', sa.JSON()))
    op.create_table('security_folder_policy', sa.Column('key', sa.String(200), primary_key=True),
                    sa.Column('machine_id', sa.String(32), nullable=False), sa.Column('account', sa.String(32), nullable=False),
                    sa.Column('roots', sa.JSON(), nullable=False), sa.Column('revision', sa.Integer(), nullable=False),
                    sa.Column('applied_revision', sa.Integer(), nullable=False), sa.Column('state', sa.String(20), nullable=False),
                    sa.Column('detail', sa.JSON(), nullable=False), sa.Column('updated_by', sa.String(64)),
                    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False))
    op.create_table('security_revision', sa.Column('id', sa.Integer(), primary_key=True),
                    sa.Column('revision', sa.Integer(), nullable=False))
    op.create_table('security_change', sa.Column('id', sa.String(32), primary_key=True),
                    sa.Column('actor', sa.String(64), nullable=False), sa.Column('session_hash', sa.String(64), nullable=False),
                    sa.Column('digest', sa.String(64), nullable=False), sa.Column('payload', sa.JSON(), nullable=False),
                    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
                    sa.Column('applied', sa.Boolean(), nullable=False))
    op.bulk_insert(sa.table('security_revision', sa.column('id', sa.Integer()), sa.column('revision', sa.Integer())),
                   [{'id': 1, 'revision': 0}])
    # Keep all legacy settings intact. The resolver imports their explicit choices
    # as a compatibility baseline, including person-wide tool exemptions on nodes.
    # Never backfill defaults over missing/explicit-false owner choices.


def downgrade():
    # An older binary cannot enforce scoped choices. Restore a pre-upgrade backup
    # rather than silently throwing away policy or restoring permissive defaults.
    if (op.get_bind().scalar(sa.text('SELECT count(*) FROM security_policy'))
            or op.get_bind().scalar(sa.text('SELECT count(*) FROM security_folder_policy'))):
        raise RuntimeError('Scoped security choices exist; restore the pre-upgrade backup before downgrading.')
    op.drop_table('security_change')
    op.drop_table('security_revision')
    op.drop_table('security_folder_policy')
    op.drop_table('security_policy')
