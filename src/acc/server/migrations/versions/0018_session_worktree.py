"""session worktree: a project session can run in its own git worktree and branch

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-22 02:30:00
"""
import sqlalchemy as sa
from alembic import op

revision = '0018'
down_revision = '0017'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('session') as batch:
        batch.add_column(sa.Column('worktree', sa.Text(), nullable=True))
        batch.add_column(sa.Column('worktree_branch', sa.String(length=120), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('session') as batch:
        batch.drop_column('worktree_branch')
        batch.drop_column('worktree')
