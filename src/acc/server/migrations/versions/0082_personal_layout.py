"""Personal presentation layouts; reset tombstones retain monotonic revisions."""
import sqlalchemy as sa
from alembic import op

revision = '0082'
down_revision = '0081'
branch_labels = depends_on = None


def upgrade():
    op.create_table('personal_layout',
        sa.Column('owner', sa.String(64), sa.ForeignKey('user.username', ondelete='CASCADE'), primary_key=True),
        sa.Column('project_id', sa.Integer(), sa.ForeignKey('project.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('device', sa.String(16), primary_key=True),
        sa.Column('viewport', sa.String(16), primary_key=True),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('layout', sa.JSON(none_as_null=True)))

    op.create_table('layout_file_ref',
        sa.Column('id', sa.String(32), primary_key=True),
        sa.Column('owner', sa.String(64), sa.ForeignKey('user.username', ondelete='CASCADE'), nullable=False),
        sa.Column('project_id', sa.Integer(), sa.ForeignKey('project.id', ondelete='CASCADE'), nullable=False),
        sa.Column('location', sa.String(16), nullable=False),
        sa.Column('target', sa.String(64), nullable=False),
        sa.Column('folder', sa.String(255), nullable=False, server_default=''),
        sa.Column('path', sa.String(2048), nullable=False),
        sa.Column('path_hash', sa.String(64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('owner', 'project_id', 'location', 'target', 'path_hash', name='uq_layout_file_ref'))


def downgrade():
    if op.get_bind().scalar(sa.text('SELECT count(*) FROM layout_file_ref')):
        raise RuntimeError('Layout file references exist; remove personal preference records explicitly or restore a pre-upgrade backup before downgrading.')
    if op.get_bind().scalar(sa.text('SELECT count(*) FROM personal_layout')):
        raise RuntimeError('Personal layouts exist; remove personal preference records explicitly or restore a pre-upgrade backup before downgrading.')
    op.drop_table('layout_file_ref')
    op.drop_table('personal_layout')
