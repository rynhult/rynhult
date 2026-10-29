"""settings: instance-wide and per-user choices (guardrails, usage budgets)

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-22 00:30:00
"""
import sqlalchemy as sa
from alembic import op

import acc.server.models

revision = '0016'
down_revision = '0015'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'setting',
        sa.Column('key', sa.String(length=120), nullable=False),
        sa.Column('value', sa.JSON(), nullable=False),
        sa.Column('updated_by', sa.String(length=64), nullable=True),
        sa.Column('updated_at', acc.server.models.UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint('key'),
    )


def downgrade() -> None:
    op.drop_table('setting')
