"""port registry: local ports reserved for projects or outside services

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-21 16:00:00
"""
import sqlalchemy as sa
from alembic import op

import acc.server.models

revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'port_reservation',
        sa.Column('port', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=True),
        sa.Column('label', sa.String(length=120), nullable=True),
        sa.Column('owner', sa.String(length=64), nullable=True),
        sa.Column('created_at', acc.server.models.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['project.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('port'),
    )


def downgrade() -> None:
    op.drop_table('port_reservation')
