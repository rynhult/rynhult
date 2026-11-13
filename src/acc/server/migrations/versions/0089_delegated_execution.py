"""Retained typed delegated admission and per-operation outcomes."""
import sqlalchemy as sa
from alembic import op

revision = '0089'
down_revision = '0088'
branch_labels = depends_on = None


def upgrade():
    op.create_table('delegated_action',
        sa.Column('id', sa.String(32), primary_key=True),
        sa.Column('lead_id', sa.String(32), sa.ForeignKey('session.id'), nullable=False),
        sa.Column('request_key', sa.String(32), nullable=False),
        sa.Column('action', sa.String(32), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('digest', sa.String(64), nullable=False),
        sa.Column('consent_id', sa.String(32), sa.ForeignKey('approval_consent.id'), nullable=False),
        sa.Column('consent_revision', sa.Integer(), nullable=False),
        sa.Column('decision', sa.JSON(), nullable=False),
        sa.Column('authority', sa.JSON(), nullable=False),
        sa.Column('state', sa.String(16), nullable=False),
        sa.Column('result', sa.JSON()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('lead_id', 'request_key', name='delegated_request_key'))


def downgrade():
    if op.get_bind().scalar(sa.text('SELECT count(*) FROM delegated_action')):
        raise RuntimeError('Delegated execution records exist; fence affected work and restore the pre-upgrade backup or forward-repair.')
    op.drop_table('delegated_action')
