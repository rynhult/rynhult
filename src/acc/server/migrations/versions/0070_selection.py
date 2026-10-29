"""Orchestrator v2 slice 3: selection ledger. It follows slice 1's 0069 (slice 2 needs no table)."""
import sqlalchemy as sa
from alembic import op

revision = '0070'
down_revision = '0069'  # slice 1
branch_labels = depends_on = None


def upgrade():
    op.create_table('selection',
        sa.Column('id', sa.String(32), primary_key=True),
        sa.Column('team_id', sa.String(32), sa.ForeignKey('team.id', ondelete='CASCADE'), nullable=False),
        sa.Column('requested_by', sa.String(32), sa.ForeignKey('session.id', ondelete='CASCADE'), nullable=False),
        sa.Column('request_id', sa.String(32), nullable=False),
        sa.Column('need', sa.JSON(), nullable=False), sa.Column('task_digest', sa.String(64), nullable=False),
        sa.Column('candidates', sa.JSON(), nullable=False), sa.Column('chosen', sa.Integer()),
        sa.Column('reason', sa.String(1024), nullable=False), sa.Column('budget', sa.JSON(), nullable=False),
        sa.Column('state', sa.String(16), nullable=False), sa.Column('override_by', sa.String(64)),
        sa.Column('override_reason', sa.String(1000)),
        sa.Column('session_id', sa.String(32), sa.ForeignKey('session.id', ondelete='SET NULL')),
        sa.Column('created_at', sa.DateTime(), nullable=False))
    op.create_index('ix_selection_team_id', 'selection', ['team_id'])


def downgrade():
    op.drop_index('ix_selection_team_id', table_name='selection')
    op.drop_table('selection')
