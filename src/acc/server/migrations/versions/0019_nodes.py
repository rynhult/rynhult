"""nodes: other machines that relay their brokers to this ACC (multi-host, docs/MULTIHOST.md)

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-22 01:00:00
"""
import sqlalchemy as sa
from alembic import op

import acc.server.models

revision = '0019'
down_revision = '0018'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'node',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('name', sa.String(length=40), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('public_key', sa.String(length=64), nullable=True),
        sa.Column('code_hash', sa.String(length=64), nullable=True),
        sa.Column('code_expires', acc.server.models.UTCDateTime(), nullable=True),
        sa.Column('info', sa.JSON(), nullable=False),
        sa.Column('last_seen', acc.server.models.UTCDateTime(), nullable=True),
        sa.Column('created_by', sa.String(length=64), nullable=True),
        sa.Column('created_at', acc.server.models.UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )


def downgrade() -> None:
    op.drop_table('node')
