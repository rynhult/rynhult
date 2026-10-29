"""model endpoints: local LLM servers and compatible APIs agents can use instead of their vendor's models

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-21 19:00:00
"""
import sqlalchemy as sa
from alembic import op

import acc.server.models

revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'model_endpoint',
        sa.Column('id', sa.String(length=40), nullable=False),
        sa.Column('name', sa.String(length=80), nullable=False),
        sa.Column('kind', sa.String(length=20), nullable=False),
        sa.Column('base_url', sa.String(length=300), nullable=False),
        sa.Column('styles', sa.JSON(), nullable=False),
        sa.Column('api_key', sa.Text(), nullable=True),
        sa.Column('context_tokens', sa.Integer(), nullable=True),
        sa.Column('models', sa.JSON(), nullable=False),
        sa.Column('models_at', acc.server.models.UTCDateTime(), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('created_by', sa.String(length=64), nullable=True),
        sa.Column('created_at', acc.server.models.UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('model_endpoint')
