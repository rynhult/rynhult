"""Require explicit agent accounts; retain existing assignments.

Revision ID: 0026
Revises: 0025
"""
import sqlalchemy as sa
from alembic import op

revision = '0026'
down_revision = '0025'
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table, column in [('project', 'account'), ('session', 'run_as')]:
        with op.batch_alter_table(table) as batch:
            batch.alter_column(column, existing_type=sa.String(32), server_default=None)


def downgrade() -> None:
    # Account selection must stay explicit even on rollback.
    pass
