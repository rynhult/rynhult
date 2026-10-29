"""Optional Orchestrator grants, membership and durable spool requests."""
from alembic import op
import sqlalchemy as sa
from acc.server.models import UTCDateTime
revision = '0032'
down_revision = '0031'
branch_labels = depends_on = None


def upgrade():
    op.add_column('session', sa.Column('team_stop_pending', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('session', sa.Column('team_host', sa.String(40)))
    op.add_column('session', sa.Column('team_repo', sa.Text()))
    op.add_column('session', sa.Column('team_id', sa.String(32)))
    op.create_index('ix_session_team_id', 'session', ['team_id'])
    op.add_column('session', sa.Column('parent_session_id', sa.String(32)))
    op.add_column('session', sa.Column('team_depth', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('session', sa.Column('helper_kind', sa.String(16)))
    op.add_column('session', sa.Column('team_task', sa.Text()))
    op.create_table('team', sa.Column('id', sa.String(32), primary_key=True), sa.Column('limits', sa.JSON(), nullable=False),
                    sa.Column('expires_at', UTCDateTime(), nullable=False), sa.Column('stopped', sa.Boolean(), nullable=False),
                    sa.Column('started', sa.Integer(), nullable=False))
    op.create_table('team_request', sa.Column('session_id', sa.String(32), sa.ForeignKey('session.id'), primary_key=True),
                    sa.Column('id', sa.String(32), primary_key=True), sa.Column('payload', sa.JSON(), nullable=False),
                    sa.Column('result', sa.JSON()), sa.Column('delivered', sa.Boolean(), nullable=False))


def downgrade():
    op.drop_table('team_request')
    op.drop_table('team')
    op.drop_index('ix_session_team_id', 'session')
    for column in ('team_stop_pending', 'team_host', 'team_repo', 'team_task', 'helper_kind', 'team_depth', 'parent_session_id', 'team_id'):
        op.drop_column('session', column)
