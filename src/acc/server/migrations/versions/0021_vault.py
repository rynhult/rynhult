"""vault: API keys stored once (encrypted), named and given to projects (docs/VAULT.md)

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-22 12:00:00
"""
import sqlalchemy as sa
from alembic import op

import acc.server.models

revision = '0021'
down_revision = '0020'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'vault_key',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('name', sa.String(length=80), nullable=False),
        sa.Column('variable', sa.String(length=64), nullable=False),
        sa.Column('ciphertext', sa.Text(), nullable=False),
        sa.Column('owner', sa.String(length=64), nullable=False),
        sa.Column('shared', sa.Boolean(), nullable=False),
        sa.Column('remote', sa.Boolean(), nullable=False),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('created_at', acc.server.models.UTCDateTime(), nullable=False),
        sa.Column('updated_at', acc.server.models.UTCDateTime(), nullable=False),
        sa.Column('last_used_at', acc.server.models.UTCDateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_vault_key_owner', 'vault_key', ['owner'])
    op.create_table(
        'vault_grant',
        sa.Column('key_id', sa.String(length=32), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('given_by', sa.String(length=64), nullable=False),
        sa.Column('given_at', acc.server.models.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(['key_id'], ['vault_key.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['project_id'], ['project.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('key_id', 'project_id'),
    )
    with op.batch_alter_table('session') as batch:
        batch.add_column(sa.Column('vault_keys', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('session') as batch:
        batch.drop_column('vault_keys')
    op.drop_table('vault_grant')
    op.drop_index('ix_vault_key_owner', table_name='vault_key')
    op.drop_table('vault_key')
