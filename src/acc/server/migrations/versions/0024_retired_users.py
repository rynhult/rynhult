"""retired users: a removed person's username is kept for good, so a later invitee can't choose it and inherit their
projects, sessions and keys (prerelease reviews X1/D9/Q1). Names that already own rows but have no user (people
removed before this) are retired now.

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-22 20:30:00
"""
import sqlalchemy as sa
from alembic import op

revision = '0024'
down_revision = '0023'
branch_labels = None
depends_on = None

OWNED = (("project", "owner"), ("session", "owner"), ("vault_key", "owner"), ("attention_item", "owner"),
         ("port_reservation", "owner"), ("publish_request", "owner"), ("push_subscription", "username"))


def upgrade() -> None:
    op.create_table(
        'retired_user',
        sa.Column('username', sa.String(64), primary_key=True),
        sa.Column('role', sa.String(16)),
        sa.Column('account', sa.String(32)),
        sa.Column('removed_by', sa.String(64)),
        sa.Column('resources', sa.String(16)),
        sa.Column('removed_at', sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
    )
    for table, column in OWNED:
        op.execute(f"INSERT INTO retired_user (username) SELECT DISTINCT {column} FROM {table} "
                   f"WHERE {column} IS NOT NULL AND {column} != '' AND {column} NOT LIKE '@%' "
                   f"AND {column} NOT IN (SELECT username FROM \"user\") ON CONFLICT (username) DO NOTHING")


def downgrade() -> None:
    op.drop_table('retired_user')
