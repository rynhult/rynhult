"""Durable exclusive-writer plans; no session or capability is enabled."""
import sqlalchemy as sa
from alembic import op

revision = '0093'
down_revision = '0092'
branch_labels = depends_on = None


def upgrade():
    op.create_table('relay_ownership',
        sa.Column('session_id', sa.String(32), sa.ForeignKey('relay_session.session_id', ondelete='CASCADE'), primary_key=True),
        sa.Column('mode', sa.String(32), nullable=False),
        sa.Column('view', sa.String(16), nullable=False),
        sa.Column('phase', sa.String(32), nullable=False),
        sa.Column('plan_id', sa.String(32), nullable=True),
        sa.Column('request_key', sa.String(128), nullable=True),
        sa.Column('target', sa.String(16), nullable=True),
        sa.Column('digest', sa.String(80), nullable=True),
        sa.Column('source_run_id', sa.Integer(), sa.ForeignKey('relay_run.run_id'), nullable=False),
        sa.Column('target_run_id', sa.Integer(), sa.ForeignKey('session_run.id'), nullable=True),
        sa.Column('authority', sa.JSON(), nullable=False),
        sa.Column('previous_actor', sa.JSON(), nullable=True),
        sa.Column('prepare_operation_id', sa.String(32), sa.ForeignKey('relay_operation.id'), nullable=True),
        sa.Column('commit_operation_id', sa.String(32), sa.ForeignKey('relay_operation.id'), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True))


def downgrade():
    if op.get_bind().scalar(sa.text('SELECT count(*) FROM relay_ownership')):
        raise RuntimeError('Ownership records exist; retain outcome/fencing evidence and forward-repair.')
    op.drop_table('relay_ownership')
