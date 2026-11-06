"""Create layout_file_ref on installs that applied 0082 before the table was added to it.

0082 gained layout_file_ref after it had already run on upgraded installs (#407 → #408), so those databases are
at 0084 without the table and every layout save fails. Fresh installs create it in 0082; this is then a no-op.
"""
import sqlalchemy as sa
from alembic import op

revision = '0085'
down_revision = '0084'
branch_labels = depends_on = None


def upgrade():
    if 'layout_file_ref' in sa.inspect(op.get_bind()).get_table_names():
        return
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
    # 0082's downgrade owns the table; keeping it here avoids dropping data a fresh install created in 0082.
    pass
