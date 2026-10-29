"""users: roles and agent accounts; invites

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-21 12:00:00
"""
import sqlalchemy as sa
from alembic import op

import acc.server.models

revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('user') as batch:
        batch.add_column(sa.Column('role', sa.String(length=16), server_default='member', nullable=False))
        batch.add_column(sa.Column('account', sa.String(length=32), nullable=True))
    op.execute("UPDATE \"user\" SET role = 'owner'")  # whoever set ACC up so far
    op.create_table(
        'invite',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('role', sa.String(length=16), nullable=False),
        sa.Column('account', sa.String(length=32), nullable=True),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('created_by', sa.String(length=64), nullable=False),
        sa.Column('created_at', acc.server.models.UTCDateTime(), nullable=False),
        sa.Column('expires_at', acc.server.models.UTCDateTime(), nullable=False),
        sa.Column('used_at', acc.server.models.UTCDateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('token_hash'),
    )


def downgrade() -> None:
    op.drop_table('invite')
    with op.batch_alter_table('user') as batch:
        batch.drop_column('account')
        batch.drop_column('role')
