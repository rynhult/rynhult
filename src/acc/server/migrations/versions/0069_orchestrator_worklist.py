"""Orchestrator v2, slice 1: a team's working list and NOW (docs/ORCHESTRATOR-V2.md). Numbered 0069 by the coordinator
(2026-10-26); it follows 0068 (remote-access backup forwarder), after mobile's 0067."""
import sqlalchemy as sa
from alembic import op

revision = '0069'
down_revision = '0068'
branch_labels = depends_on = None


def upgrade():
    op.create_table('orchestration_run',
                    sa.Column('team_id', sa.String(32), sa.ForeignKey('team.id'), primary_key=True),
                    sa.Column('now', sa.Text(), nullable=False),
                    sa.Column('now_updated_at', sa.DateTime(), nullable=True),
                    sa.Column('now_updated_by', sa.String(64), nullable=True),
                    sa.Column('now_revision', sa.Integer(), nullable=False),
                    sa.Column('lead_seen_now', sa.Integer(), nullable=False),
                    sa.Column('revision', sa.Integer(), nullable=False))
    op.create_table('work_item',
                    sa.Column('id', sa.Integer(), primary_key=True),
                    sa.Column('team_id', sa.String(32), sa.ForeignKey('team.id'), nullable=False),
                    sa.Column('position', sa.Float(), nullable=False),
                    sa.Column('kind', sa.String(16), nullable=False),
                    sa.Column('title', sa.String(200), nullable=False),
                    sa.Column('steps', sa.Text(), nullable=False),
                    sa.Column('options', sa.JSON(), nullable=True),
                    sa.Column('recommendation', sa.JSON(), nullable=True),
                    sa.Column('state', sa.String(16), nullable=False),
                    sa.Column('answer', sa.Text(), nullable=True),
                    sa.Column('answered_by', sa.String(64), nullable=True),
                    sa.Column('answered_at', sa.DateTime(), nullable=True),
                    sa.Column('ingested_at', sa.DateTime(), nullable=True),
                    sa.Column('done_at', sa.DateTime(), nullable=True),
                    sa.Column('note', sa.Text(), nullable=True),
                    sa.Column('source', sa.String(64), nullable=False),
                    sa.Column('owner_edited', sa.Boolean(), nullable=False),
                    sa.Column('about', sa.String(32), nullable=True),
                    sa.Column('links', sa.JSON(), nullable=False),
                    sa.Column('revision', sa.Integer(), nullable=False),
                    sa.Column('created_at', sa.DateTime(), nullable=False),
                    sa.Column('updated_at', sa.DateTime(), nullable=False))
    op.create_index('ix_work_item_team_id', 'work_item', ['team_id'])
    op.create_index('ix_work_item_about', 'work_item', ['about'])


def downgrade():
    op.drop_index('ix_work_item_about', 'work_item')
    op.drop_index('ix_work_item_team_id', 'work_item')
    op.drop_table('work_item')
    op.drop_table('orchestration_run')
