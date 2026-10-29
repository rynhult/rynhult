"""Orchestrator v2 slice 5: gate queue jobs and number reservations.

In slice order it follows slice 4's 0071: the chain is 0068 -> 0069 -> 0070 -> 0071 -> 0072.
"""
import sqlalchemy as sa
from alembic import op

revision = '0072'
down_revision = '0071'  # slice 4
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        'gate_job',
        sa.Column('id', sa.String(32), primary_key=True),
        sa.Column('project_id', sa.Integer(), sa.ForeignKey('project.id', ondelete='CASCADE'), nullable=False),
        sa.Column('team_id', sa.String(32)),
        sa.Column('branch', sa.String(100), nullable=False),
        sa.Column('base', sa.String(100), nullable=False),
        sa.Column('stack_parent', sa.String(32)),
        sa.Column('head', sa.String(64)),
        sa.Column('suite', sa.String(16), nullable=False),
        sa.Column('priority', sa.String(8), nullable=False),
        sa.Column('state', sa.String(16), nullable=False),
        sa.Column('waiting', sa.String(32)),
        sa.Column('slot', sa.Integer()),
        sa.Column('attempt', sa.Integer(), nullable=False),
        sa.Column('host', sa.String(64), nullable=False),
        sa.Column('account', sa.String(32), nullable=False),
        sa.Column('pid_record', sa.JSON()),
        sa.Column('log_path', sa.String(300)),
        sa.Column('requested_by', sa.String(80), nullable=False),
        sa.Column('reason', sa.String(500)),
        sa.Column('results', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('started_at', sa.DateTime()),
        sa.Column('finished_at', sa.DateTime()),
    )
    op.create_index('ix_gate_job_project_id', 'gate_job', ['project_id'])
    op.create_index('ix_gate_job_team_id', 'gate_job', ['team_id'])
    op.create_index('ix_gate_job_state', 'gate_job', ['state'])
    op.create_table(
        'reservation',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('kind', sa.String(16), nullable=False),
        sa.Column('number', sa.Integer(), nullable=False),
        sa.Column('team_id', sa.String(32)),
        sa.Column('session_id', sa.String(32), sa.ForeignKey('session.id', ondelete='SET NULL')),
        sa.Column('branch', sa.String(100)),
        sa.Column('state', sa.String(16), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('kind', 'number', name='uq_reservation_number'),
    )
    op.create_index('ix_reservation_team_id', 'reservation', ['team_id'])


def downgrade():
    op.drop_index('ix_reservation_team_id', 'reservation')
    op.drop_table('reservation')
    op.drop_index('ix_gate_job_state', 'gate_job')
    op.drop_index('ix_gate_job_team_id', 'gate_job')
    op.drop_index('ix_gate_job_project_id', 'gate_job')
    op.drop_table('gate_job')
