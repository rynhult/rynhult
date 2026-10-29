"""api principal: what the bootstrap bearer token creates was owned by "api", a name anyone invited could sign up
as and so inherit (prerelease reviews X1/D7). It is owned by "@api" now, which can't be a username.

A person who already signed up as "api" can't be told apart from the token in old rows: those rows move to "@api"
too (owners still see and manage them), and nobody can sign up as "api" any more.

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-22 20:00:00
"""
from alembic import op

revision = '0023'
down_revision = '0022'
branch_labels = None
depends_on = None

OWNED = (("project", "owner"), ("session", "owner"), ("vault_key", "owner"), ("attention_item", "owner"),
         ("port_reservation", "owner"), ("publish_request", "owner"), ("push_subscription", "username"))


def upgrade() -> None:
    for table, column in OWNED:
        op.execute(f"UPDATE {table} SET {column} = '@api' WHERE {column} = 'api'")
    op.execute("UPDATE vault_grant SET given_by = '@api' WHERE given_by = 'api'")
    op.execute("UPDATE setting SET key = 'budget.@api' WHERE key = 'budget.api' "
               "AND NOT EXISTS (SELECT 1 FROM setting WHERE key = 'budget.@api')")


def downgrade() -> None:
    for table, column in OWNED:
        op.execute(f"UPDATE {table} SET {column} = 'api' WHERE {column} = '@api'")
    op.execute("UPDATE vault_grant SET given_by = 'api' WHERE given_by = '@api'")
    op.execute("UPDATE setting SET key = 'budget.api' WHERE key = 'budget.@api'")
