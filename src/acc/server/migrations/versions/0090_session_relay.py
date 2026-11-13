"""Durable session relay foundation; no existing session is enrolled."""
import sqlalchemy as sa
from alembic import op
revision = '0090'
down_revision = '0089'
branch_labels = depends_on = None

def upgrade():
    op.create_table('relay_session',
        sa.Column('active_operation_id', sa.String(32), nullable=True),
        sa.Column('session_id', sa.String(32), sa.ForeignKey('session.id', ondelete='CASCADE'), nullable=False, primary_key=True),
        sa.Column('enrollment', sa.String(32), nullable=False),
        sa.Column('current_run_id', sa.Integer(), sa.ForeignKey('session_run.id'), nullable=True),
        sa.Column('binding_id', sa.String(128), nullable=False),
        sa.Column('history_epoch', sa.Integer(), nullable=False),
        sa.Column('event_seq', sa.Integer(), nullable=False),
        sa.Column('event_count', sa.Integer(), nullable=False),
        sa.Column('used_bytes', sa.Integer(), nullable=False),
        sa.Column('floor_seq', sa.Integer(), nullable=False),
        sa.Column('authority_epoch', sa.Integer(), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('configuration_revision', sa.Integer(), nullable=False),
        sa.Column('control_revision', sa.Integer(), nullable=False),
        sa.Column('controller', sa.JSON(), nullable=True),
        sa.Column('capabilities', sa.JSON(), nullable=False),
        sa.Column('sealed', sa.Boolean(), nullable=False))
    op.create_table('relay_run',
        sa.Column('run_id', sa.Integer(), sa.ForeignKey('session_run.id'), nullable=False, primary_key=True),
        sa.Column('session_id', sa.String(32), sa.ForeignKey('relay_session.session_id', ondelete='CASCADE'), nullable=False),
        sa.Column('binding_id', sa.String(128), nullable=False),
        sa.Column('identity', sa.JSON(), nullable=False),
        sa.Column('frozen_policy', sa.JSON(), nullable=False),
        sa.Column('policy_revision', sa.Integer(), nullable=False))
    op.create_index('ix_relay_run_session_id', 'relay_run', ['session_id'])
    op.create_table('relay_operation',
        sa.Column('id', sa.String(32), nullable=False, primary_key=True),
        sa.Column('session_id', sa.String(32), sa.ForeignKey('relay_session.session_id', ondelete='CASCADE'), nullable=False),
        sa.Column('run_id', sa.Integer(), sa.ForeignKey('relay_run.run_id'), nullable=False),
        sa.Column('actor_key', sa.String(256), nullable=False),
        sa.Column('registration_key', sa.String(128), nullable=False),
        sa.Column('receipt', sa.JSON(), nullable=False),
        sa.Column('authority', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('deadline', sa.DateTime(timezone=True), nullable=False),
        sa.Column('queue_seq', sa.Integer(), nullable=True),
        sa.Column('confirmed', sa.Boolean(), nullable=False),
        sa.Column('configuration_revision', sa.Integer(), nullable=False),
        sa.UniqueConstraint('session_id', 'actor_key', 'registration_key', name='relay_registration'))
    op.create_index('ix_relay_operation_session_id', 'relay_operation', ['session_id'])
    op.create_table('relay_event',
        sa.Column('session_id', sa.String(32), sa.ForeignKey('relay_session.session_id', ondelete='CASCADE'), nullable=False, primary_key=True),
        sa.Column('seq', sa.Integer(), nullable=False, primary_key=True),
        sa.Column('source_key', sa.String(256), nullable=True),
        sa.Column('byte_size', sa.Integer(), nullable=False),
        sa.Column('event', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('published', sa.Boolean(), nullable=False),
        sa.UniqueConstraint('session_id', 'source_key', name='relay_source_event'))
    op.create_table('relay_decision',
        sa.Column('session_id', sa.String(32), sa.ForeignKey('relay_session.session_id', ondelete='CASCADE'), nullable=False, primary_key=True),
        sa.Column('id', sa.String(128), nullable=False, primary_key=True),
        sa.Column('run_id', sa.Integer(), sa.ForeignKey('relay_run.run_id'), nullable=False),
        sa.Column('decision', sa.JSON(), nullable=False))
    op.create_table('relay_preference',
        sa.Column('owner', sa.String(64), sa.ForeignKey('user.username', ondelete='CASCADE'), nullable=False, primary_key=True),
        sa.Column('view', sa.String(16), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False))

def downgrade():
    # Retained receipts and deduplication keys cannot be discarded by a rollback.
    for table in ('relay_operation', 'relay_decision', 'relay_session', 'relay_preference'):
        if op.get_bind().scalar(sa.text('SELECT count(*) FROM ' + table)):
            raise RuntimeError('Relay records exist; retain outcomes and forward-repair or restore the pre-upgrade backup.')
    op.drop_table('relay_preference')
    op.drop_table('relay_decision')
    op.drop_table('relay_event')
    op.drop_table('relay_operation')
    op.drop_table('relay_run')
    op.drop_table('relay_session')
