"""git backup: each project's backup to the main ACC can be turned off (on by default; docs/MULTIHOST.md)

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-22 18:00:00
"""
import sqlalchemy as sa
from alembic import op

revision = '0022'
down_revision = '0021'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('project') as batch_op:
        batch_op.add_column(sa.Column('git_backup', sa.Boolean(), server_default='1', nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('project') as batch_op:
        batch_op.drop_column('git_backup')
