"""Orchestrator v2, slice 6: the planning session and the roadmap, person edits to the plan, and a working-list item's
default until answered (docs/ORCHESTRATOR-V2.md). Numbered 0073 by the coordinator (2026-10-26); it follows slice
5's 0072."""
import sqlalchemy as sa
from alembic import op

revision = '0073'
down_revision = '0072'  # slice 5
branch_labels = depends_on = None


def upgrade():
    op.create_table('orchestration_plan',
                    sa.Column('team_id', sa.String(32), sa.ForeignKey('team.id'), primary_key=True),
                    sa.Column('state', sa.String(16), nullable=False),
                    sa.Column('idea', sa.Text(), nullable=False),
                    sa.Column('scope', sa.JSON(), nullable=False),
                    sa.Column('version', sa.Integer(), nullable=False),
                    sa.Column('person_edits', sa.JSON(), nullable=False),
                    sa.Column('lead_seen_version', sa.Integer(), nullable=False),
                    sa.Column('ready_at', sa.DateTime(), nullable=True),
                    sa.Column('updated_at', sa.DateTime(), nullable=False))
    op.create_table('plan_phase',
                    sa.Column('id', sa.Integer(), primary_key=True),
                    sa.Column('team_id', sa.String(32), sa.ForeignKey('team.id'), nullable=False),
                    sa.Column('key', sa.String(32), nullable=False),
                    sa.Column('position', sa.Float(), nullable=False),
                    sa.Column('title', sa.String(200), nullable=False),
                    sa.Column('summary', sa.Text(), nullable=False),
                    sa.Column('check', sa.Text(), nullable=False),
                    sa.Column('status', sa.String(16), nullable=False),
                    sa.Column('depends_on', sa.JSON(), nullable=False),
                    sa.UniqueConstraint('team_id', 'key', name='uq_plan_phase_key'))
    op.create_index('ix_plan_phase_team_id', 'plan_phase', ['team_id'])
    op.create_table('plan_version',
                    sa.Column('id', sa.Integer(), primary_key=True),
                    sa.Column('team_id', sa.String(32), sa.ForeignKey('team.id'), nullable=False),
                    sa.Column('version', sa.Integer(), nullable=False),
                    sa.Column('reason', sa.String(500), nullable=False),
                    sa.Column('actor', sa.String(64), nullable=False),
                    sa.Column('snapshot', sa.JSON(), nullable=False),
                    sa.Column('created_at', sa.DateTime(), nullable=False),
                    sa.UniqueConstraint('team_id', 'version', name='uq_plan_version'))
    op.create_index('ix_plan_version_team_id', 'plan_version', ['team_id'])
    with op.batch_alter_table('work_item') as batch:
        batch.add_column(sa.Column('default_answer', sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table('work_item') as batch:
        batch.drop_column('default_answer')
    op.drop_index('ix_plan_version_team_id', 'plan_version')
    op.drop_table('plan_version')
    op.drop_index('ix_plan_phase_team_id', 'plan_phase')
    op.drop_table('plan_phase')
    op.drop_table('orchestration_plan')
