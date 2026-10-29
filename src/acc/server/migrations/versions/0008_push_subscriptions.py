"""push subscriptions (Web Push devices)

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-18 20:00:00
"""
import sqlalchemy as sa
from alembic import op

import acc.server.models

revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'push_subscription',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('endpoint', sa.Text(), nullable=False),
        sa.Column('p256dh', sa.String(length=200), nullable=False),
        sa.Column('auth', sa.String(length=64), nullable=False),
        sa.Column('username', sa.String(length=64), nullable=False),
        sa.Column('label', sa.String(length=120), nullable=False),
        sa.Column('created_at', acc.server.models.UTCDateTime(), nullable=False),
        sa.Column('last_ok_at', acc.server.models.UTCDateTime(), nullable=True),
        sa.Column('failures', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('endpoint'),
    )


def downgrade() -> None:
    op.drop_table('push_subscription')
