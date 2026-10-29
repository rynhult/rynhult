"""Person-launched groups of independent sessions."""
import sqlalchemy as sa
from alembic import op

from acc.server.models import UTCDateTime

revision = '0037'
down_revision = '0036'
branch_labels = depends_on = None


def upgrade():
    op.create_table('multirun',
                    sa.Column('id', sa.String(32), primary_key=True),
                    sa.Column('project_id', sa.Integer(), sa.ForeignKey('project.id'), nullable=False),
                    sa.Column('owner', sa.String(64)),
                    sa.Column('name', sa.String(40), nullable=False),
                    sa.Column('winner_session_id', sa.String(32)),
                    sa.Column('created_at', UTCDateTime(), nullable=False))
    op.create_index('ix_multirun_owner', 'multirun', ['owner'])
    op.add_column('session', sa.Column('multirun_id', sa.String(32)))
    op.add_column('session', sa.Column('multirun_stop_pending', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_index('ix_session_multirun_id', 'session', ['multirun_id'])
    op.create_table('multirun_entry',
                    sa.Column('group_id', sa.String(32), sa.ForeignKey('multirun.id'), primary_key=True),
                    sa.Column('position', sa.Integer(), primary_key=True),
                    sa.Column('choice', sa.JSON(), nullable=False),
                    sa.Column('session_id', sa.String(32), sa.ForeignKey('session.id')),
                    sa.Column('error', sa.Text()))


def downgrade():
    op.drop_table('multirun_entry')
    op.drop_index('ix_session_multirun_id', 'session')
    op.drop_column('session', 'multirun_id')
    op.drop_column('session', 'multirun_stop_pending')
    op.drop_index('ix_multirun_owner', 'multirun')
    op.drop_table('multirun')
