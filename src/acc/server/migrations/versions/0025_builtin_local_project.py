"""builtin local project: the project grouping an account's local workspaces was found by its slug ("local",
"local-<account>"), which anyone could register first and so receive another person's local sessions (prerelease
review X2). It is marked `builtin = 'local'` now and found by account and host; the slugs are reserved.

Rows that look like ACC made them (its name and note, this machine, a folder ending in /local, the slug matching the
account) are marked. Any other project holding one of those slugs is renamed out of the way, with an audit line.

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-22 21:00:00
"""
import json

import sqlalchemy as sa
from alembic import op

revision = '0025'
down_revision = '0024'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('project') as batch_op:
        batch_op.add_column(sa.Column('builtin', sa.String(16), nullable=True))
    db = op.get_bind()
    db.execute(sa.text(
        "UPDATE project SET builtin = 'local' WHERE (slug = 'local' OR slug = 'local-' || account) "
        "AND name = 'Local workspaces' AND notes LIKE 'Built in: local workspaces%' AND host = 'local' "
        "AND workspace_path LIKE '%/local'"))
    for pid, slug in db.execute(sa.text("SELECT id, slug FROM project WHERE builtin IS NULL "
                                        "AND (slug = 'local' OR slug LIKE 'local-%')")).all():
        new = f"renamed-{slug}-{pid}"[:64]
        db.execute(sa.text("UPDATE project SET slug = :new WHERE id = :id"), {"new": new, "id": pid})
        db.execute(sa.text("INSERT INTO audit_log (ts, actor, action, target, detail) VALUES "
                           "(CURRENT_TIMESTAMP, 'acc', 'project.renamed', :new, :detail)"),
                   {"new": new, "detail": json.dumps({"from": slug, "reason": "reserved for local workspaces"})})


def downgrade() -> None:
    with op.batch_alter_table('project') as batch_op:
        batch_op.drop_column('builtin')
