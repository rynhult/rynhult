"""Orchestrator v2 slice 4: durable delivery evidence and authenticated task reports.

It follows slice 3's 0070.
"""
import sqlalchemy as sa
from alembic import op

revision = '0071'
down_revision = '0070'  # slice 3
branch_labels = depends_on = None


def upgrade():
    op.create_table('message_delivery',
        sa.Column('id', sa.String(32), primary_key=True),
        sa.Column('session_id', sa.String(32), sa.ForeignKey('session.id'), nullable=False),
        sa.Column('actor', sa.String(80), nullable=False), sa.Column('preview', sa.String(200), nullable=False),
        sa.Column('text_sha256', sa.String(64), nullable=False), sa.Column('bytes', sa.Integer, nullable=False),
        sa.Column('body', sa.Text, nullable=False), sa.Column('state', sa.String(16), nullable=False),
        sa.Column('ack', sa.String(40)), sa.Column('attempts', sa.Integer, nullable=False),
        sa.Column('error', sa.String(48)), sa.Column('created_at', sa.DateTime, nullable=False),
        sa.Column('updated_at', sa.DateTime, nullable=False), sa.Column('deliver_after', sa.DateTime, nullable=False),
        sa.Column('expires_at', sa.DateTime, nullable=False))
    op.create_index('ix_message_delivery_session_id', 'message_delivery', ['session_id'])
    op.create_index('ix_message_delivery_state', 'message_delivery', ['state'])
    op.create_table('task_report',
        sa.Column('session_id', sa.String(32), sa.ForeignKey('session.id'), primary_key=True),
        sa.Column('task_id', sa.String(64), primary_key=True),
        sa.Column('team_id', sa.String(32), sa.ForeignKey('team.id'), nullable=False),
        sa.Column('status', sa.String(16), nullable=False), sa.Column('summary', sa.Text, nullable=False),
        sa.Column('changed', sa.JSON, nullable=False), sa.Column('tests', sa.JSON, nullable=False),
        sa.Column('branch', sa.String(160)), sa.Column('head', sa.String(64)), sa.Column('handoff_path', sa.Text),
        sa.Column('needs', sa.JSON, nullable=False), sa.Column('needs_ingested', sa.Boolean, nullable=False),
        sa.Column('received_at', sa.DateTime, nullable=False))
    op.create_index('ix_task_report_team_id', 'task_report', ['team_id'])


def downgrade():
    op.drop_table('task_report')
    op.drop_table('message_delivery')
