"""Bind verification evidence and bound the expired lead's wrap-up."""
from datetime import timedelta

import sqlalchemy as sa
from alembic import op

revision = '0074'
down_revision = '0073'
branch_labels = depends_on = None


def upgrade():
    op.execute("UPDATE gate_job SET state = 'retry-passed' WHERE state = 'flaky-passed'")
    op.add_column('team', sa.Column('wrapup_until', sa.DateTime(), nullable=True))
    for name, size in (('base_sha', 64), ('commands_digest', 64), ('runner_version', 80)):
        op.add_column('gate_job', sa.Column(name, sa.String(size), nullable=True))
    op.add_column('task_report', sa.Column('reviewed_head', sa.String(64), nullable=True))
    op.add_column('task_report', sa.Column('reviewed_branch', sa.String(160), nullable=True))
    # Earlier expiry stopped helpers but left the lead running forever. Backfill
    # those leads against the original expiry; the supervisor finishes overdue wraps.
    conn = op.get_bind()
    team = sa.table('team', sa.column('id'), sa.column('stopped'), sa.column('expires_at', sa.DateTime()), sa.column('wrapup_until', sa.DateTime()))
    session = sa.table('session', sa.column('id'), sa.column('desired_state'))
    rows = list(conn.execute(sa.select(team.c.id, team.c.expires_at).join(session, session.c.id == team.c.id)
                            .where(team.c.stopped == True, session.c.desired_state == 'running')))
    for identifier, expires in rows:
        conn.execute(team.update().where(team.c.id == identifier).values(wrapup_until=expires + timedelta(minutes=30)))


def downgrade():
    for name in ('reviewed_branch', 'reviewed_head'):
        op.drop_column('task_report', name)
    for name in ('runner_version', 'commands_digest', 'base_sha'):
        op.drop_column('gate_job', name)
    op.drop_column('team', 'wrapup_until')
