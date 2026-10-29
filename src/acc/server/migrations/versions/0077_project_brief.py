"""Project-owned brief versions, private references and run pins (lane E)."""
import sqlalchemy as sa
from alembic import op

revision = '0077'
down_revision = '0076'
branch_labels = depends_on = None


def upgrade():
    op.create_table('project_brief',
        sa.Column('project_id', sa.Integer(), sa.ForeignKey('project.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('version', sa.Integer(), nullable=False), sa.Column('next_version', sa.Integer(), nullable=False),
        sa.Column('next_reference', sa.Integer(), nullable=False), sa.Column('export_path', sa.String(300), nullable=False),
        sa.Column('export_digest', sa.String(64)))
    op.create_table('project_brief_version',
        sa.Column('project_id', sa.Integer(), sa.ForeignKey('project.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('version', sa.Integer(), primary_key=True), sa.Column('base_version', sa.Integer(), nullable=False),
        sa.Column('submitted', sa.JSON(), nullable=False), sa.Column('references', sa.JSON(), nullable=False),
        sa.Column('state', sa.String(16), nullable=False), sa.Column('conflicts', sa.JSON(), nullable=False),
        sa.Column('original_markdown', sa.Text()), sa.Column('actor', sa.String(64), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False))
    op.create_table('project_brief_reference',
        sa.Column('project_id', sa.Integer(), sa.ForeignKey('project.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('reference_id', sa.String(16), primary_key=True), sa.Column('caption', sa.Text(), nullable=False),
        sa.Column('guidance', sa.Text(), nullable=False), sa.Column('url', sa.Text()),
        sa.Column('media_type', sa.String(32)), sa.Column('blob', sa.String(32)), sa.Column('sha256', sa.String(64)),
        sa.Column('size', sa.Integer(), nullable=False))
    op.create_table('team_project_brief',
        sa.Column('team_id', sa.String(32), sa.ForeignKey('team.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('project_id', sa.Integer(), sa.ForeignKey('project.id', ondelete='CASCADE'), nullable=False),
        sa.Column('brief_version', sa.Integer(), nullable=False), sa.Column('run_mode', sa.String(16), nullable=False),
        sa.Column('planning_drafted', sa.Boolean(), nullable=False),
        sa.Column('assignments', sa.JSON(), nullable=False))
    op.create_table('project_brief_interpretation',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('team_id', sa.String(32), sa.ForeignKey('team.id', ondelete='CASCADE'), nullable=False),
        sa.Column('brief_version', sa.Integer(), nullable=False), sa.Column('interpretation', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False))
    op.create_table('project_brief_change',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('project_id', sa.Integer(), sa.ForeignKey('project.id', ondelete='CASCADE'), nullable=False),
        sa.Column('previous_version', sa.Integer(), nullable=False), sa.Column('brief_version', sa.Integer(), nullable=False),
        sa.Column('fields', sa.JSON(), nullable=False), sa.Column('ingested', sa.Boolean(), nullable=False))
    op.create_index('ix_project_brief_interpretation_team_id', 'project_brief_interpretation', ['team_id'])


def downgrade():
    for table in ('project_brief_change', 'project_brief_interpretation', 'team_project_brief', 'project_brief_reference',
                  'project_brief_version', 'project_brief'):
        op.drop_table(table)
