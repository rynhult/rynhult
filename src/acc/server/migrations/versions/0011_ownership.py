"""ownership: projects, sessions, inbox items and publish requests belong to a user

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-21 14:00:00
"""
import sqlalchemy as sa
from alembic import op

revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None

TABLES = ('project', 'session', 'attention_item', 'publish_request')


def upgrade() -> None:
    for table in TABLES:
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column('owner', sa.String(length=64), nullable=True))
            batch.create_index(f'ix_{table}_owner', ['owner'])
    # Everything so far belongs to the first owner (single-user ACC); host-wide inbox items stay ownerless.
    first = "(SELECT username FROM \"user\" WHERE role = 'owner' ORDER BY id LIMIT 1)"
    for table in ('project', 'session', 'publish_request'):
        op.execute(f"UPDATE {table} SET owner = {first}")
    op.execute(f"UPDATE attention_item SET owner = {first} WHERE session_id IS NOT NULL")


def downgrade() -> None:
    for table in TABLES:
        with op.batch_alter_table(table) as batch:
            batch.drop_index(f'ix_{table}_owner')
            batch.drop_column('owner')
