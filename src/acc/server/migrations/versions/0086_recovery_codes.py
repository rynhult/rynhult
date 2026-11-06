"""Offline sign-in recovery codes."""
import sqlalchemy as sa
from alembic import op

revision = '0086'
down_revision = '0085'
branch_labels = depends_on = None


def upgrade():
    op.create_table('recovery_code',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('user.id', ondelete='CASCADE'), nullable=False),
        sa.Column('salt', sa.String(32), nullable=False),
        sa.Column('code_hash', sa.String(64), nullable=False))
    op.create_index('ix_recovery_code_user_id', 'recovery_code', ['user_id'])


def downgrade():
    op.drop_table('recovery_code')
