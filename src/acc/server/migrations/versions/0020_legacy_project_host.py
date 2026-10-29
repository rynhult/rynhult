"""legacy project host: before v0.9.0 a project's host defaulted to an installation-specific name, meaning this machine. With several
machines only "local" is this one and any other name is an enrolled node, so a host that names no node becomes
"local" (otherwise its sessions are sent to a machine that doesn't exist).

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-22 03:30:00
"""
from alembic import op

revision = '0020'
down_revision = '0019'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE project SET host = 'local' WHERE host != 'local' AND host NOT IN (SELECT name FROM node)")


def downgrade() -> None:
    pass  # the previous machine names aren't kept; "local" means the same machine
