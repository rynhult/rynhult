"""publish context: what a request would replace and whose port it is (D18)

Revision ID: 0028
Revises: 0027
Create Date: 2026-10-22 21:00:00
"""
import sqlalchemy as sa
from alembic import op

revision = '0028'
down_revision = '0027'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('publish_request') as batch_op:
        batch_op.add_column(sa.Column('context', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('publish_request') as batch_op:
        batch_op.drop_column('context')
