"""Person-owned schedules and durable run intents."""
import sqlalchemy as sa
from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('schedule',
        sa.Column('id', sa.String(32), primary_key=True),
        sa.Column('owner', sa.String(64), nullable=False),
        sa.Column('account', sa.String(32), nullable=False),
        sa.Column('launch', sa.JSON(), nullable=False),
        sa.Column('timing', sa.JSON(), nullable=False),
        sa.Column('timezone', sa.String(100), nullable=False),
        sa.Column('missed', sa.String(8), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('deleted', sa.Boolean(), nullable=False),
        sa.Column('next_run_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False))
    op.create_index('ix_schedule_owner', 'schedule', ['owner'])
    op.create_index('ix_schedule_next_run_at', 'schedule', ['next_run_at'])
    op.create_table('schedule_run',
        sa.Column('id', sa.String(32), primary_key=True),
        sa.Column('schedule_id', sa.String(32), sa.ForeignKey('schedule.id'), nullable=False),
        sa.Column('due_at', sa.DateTime(), nullable=False),
        sa.Column('trigger', sa.String(8), nullable=False),
        sa.Column('launch', sa.JSON(), nullable=False),
        sa.Column('reserved_session_id', sa.String(32), nullable=False, unique=True),
        sa.Column('session_id', sa.String(32), sa.ForeignKey('session.id'), nullable=True, unique=True),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False))
    op.create_index('ix_schedule_run_schedule_id', 'schedule_run', ['schedule_id'])
    op.create_index('ix_schedule_run_status', 'schedule_run', ['status'])


def downgrade():
    op.drop_table('schedule_run')
    op.drop_table('schedule')
