"""agent accounts: each project and session belongs to one (per-account brokers)

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-21 10:00:00
"""
import sqlalchemy as sa
from alembic import op

revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('project') as batch:
        batch.add_column(sa.Column('account', sa.String(length=32), server_default='josh', nullable=False))
    with op.batch_alter_table('session') as batch:
        batch.add_column(sa.Column('run_dir', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('session') as batch:
        batch.drop_column('run_dir')
    with op.batch_alter_table('project') as batch:
        batch.drop_column('account')
