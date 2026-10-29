"""vault retired values: what sessions were given before a key was replaced or deleted, for redaction (D22)

Revision ID: 0029
Revises: 0028
Create Date: 2026-10-22 22:00:00
"""
import sqlalchemy as sa
from alembic import op

import acc.server.models

revision = '0029'
down_revision = '0028'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'vault_retired',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('key_id', sa.String(length=32), nullable=False),
        sa.Column('name', sa.String(length=80), nullable=False),
        sa.Column('variable', sa.String(length=64), nullable=False),
        sa.Column('ciphertext', sa.Text(), nullable=False),
        sa.Column('retired_at', acc.server.models.UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('vault_retired') as batch_op:
        batch_op.create_index(batch_op.f('ix_vault_retired_key_id'), ['key_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('vault_retired') as batch_op:
        batch_op.drop_index(batch_op.f('ix_vault_retired_key_id'))
    op.drop_table('vault_retired')
