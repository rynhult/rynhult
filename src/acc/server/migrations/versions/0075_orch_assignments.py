"""Assignment identity, attributable attempts, selection provenance and report history.

Based independently on main's 0073. Integration must reparent to lane B's 0074;
that migration is intentionally not invented or duplicated in this lane.
"""
import json

import sqlalchemy as sa
from alembic import op

revision = '0075'
down_revision = '0074'
branch_labels = depends_on = None


def upgrade():
    with op.batch_alter_table('team') as batch:
        batch.add_column(sa.Column('attempt_counter', sa.Integer(), nullable=False, server_default='0'))
        batch.add_column(sa.Column('protocol_version', sa.Integer(), nullable=False, server_default='1'))
    # Freeze the issued protocol, not the current environment: legacy grants keep
    # M17 starts when the installation later enables v2 by default.
    connection = op.get_bind()
    team = sa.table('team', sa.column('id'), sa.column('protocol_version', sa.Integer()))
    session = sa.table('session', sa.column('id'), sa.column('launch_spec_id'))
    launch = sa.table('launch_spec', sa.column('id'), sa.column('files', sa.JSON()))
    ledger = sa.table('selection', sa.column('team_id'))
    recorded = set(connection.execute(sa.select(ledger.c.team_id)).scalars())
    for identity, files in connection.execute(sa.select(team.c.id, launch.c.files).select_from(
            team.join(session, team.c.id == session.c.id).join(launch, launch.c.id == session.c.launch_spec_id))):
        issued = identity in recorded
        for name, content in (files or {}).items():
            try:
                if name == 'codex-overrides.json':
                    args = []
                    for line in json.loads(content):
                        if isinstance(line, str) and line.startswith('mcp_servers.acc-team.args='):
                            args = json.loads(line.split('=', 1)[1])
                elif name.endswith('mcp.json'):
                    args = json.loads(content).get('mcpServers', {}).get('acc-team', {}).get('args', [])
                else:
                    continue
                issued = issued or isinstance(args, list) and '--orchestrator' in args
            except (TypeError, ValueError, AttributeError):
                continue
        if issued:
            connection.execute(team.update().where(team.c.id == identity).values(protocol_version=2))
    op.create_table('orch_assignment',
        sa.Column('id', sa.String(32), primary_key=True),
        sa.Column('team_id', sa.String(32), sa.ForeignKey('team.id'), nullable=False),
        sa.Column('created_by', sa.String(32), sa.ForeignKey('session.id'), nullable=False),
        sa.Column('task_type', sa.String(32), nullable=False), sa.Column('goal', sa.Text(), nullable=False),
        sa.Column('acceptance', sa.JSON(), nullable=False), sa.Column('requirement_ids', sa.JSON(), nullable=False),
        sa.Column('plan_version', sa.Integer(), nullable=False), sa.Column('brief_version', sa.Integer()),
        sa.Column('need', sa.JSON(), nullable=False), sa.Column('author_session', sa.String(32), sa.ForeignKey('session.id')),
        sa.Column('state', sa.String(24), nullable=False), sa.Column('verified_head', sa.String(64)),
        sa.Column('created_at', sa.DateTime(), nullable=False))
    op.create_index('ix_orch_assignment_team_id', 'orch_assignment', ['team_id'])
    op.create_table('orch_attempt',
        sa.Column('id', sa.String(32), primary_key=True),
        sa.Column('assignment_id', sa.String(32), sa.ForeignKey('orch_assignment.id'), nullable=False),
        sa.Column('session_id', sa.String(32), sa.ForeignKey('session.id')),
        sa.Column('selection_id', sa.String(32), sa.ForeignKey('selection.id')),
        sa.Column('sequence', sa.Integer(), nullable=False),
        sa.Column('state', sa.String(16), nullable=False), sa.Column('brief', sa.JSON(), nullable=False),
        sa.Column('branch', sa.String(160)), sa.Column('head', sa.String(64)), sa.Column('handoff_path', sa.Text()),
        sa.Column('merged', sa.Boolean(), nullable=False), sa.Column('checkout', sa.JSON()),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('selection_id', name='uq_orch_attempt_selection'),
        sa.UniqueConstraint('session_id', 'sequence', name='uq_orch_attempt_session_sequence'))
    op.create_index('ix_orch_attempt_assignment_id', 'orch_attempt', ['assignment_id'])
    op.create_index('ix_orch_attempt_session_id', 'orch_attempt', ['session_id'])
    with op.batch_alter_table('selection') as batch:
        batch.add_column(sa.Column('assignment_id', sa.String(32), nullable=True))
        batch.create_foreign_key('fk_selection_assignment', 'orch_assignment', ['assignment_id'], ['id'])
        batch.create_index('ix_selection_assignment_id', ['assignment_id'])
        batch.add_column(sa.Column('catalog_revision', sa.String(64)))
        batch.add_column(sa.Column('profile_digest', sa.String(64)))
        batch.add_column(sa.Column('classification', sa.JSON(), nullable=False, server_default='{}'))
        batch.add_column(sa.Column('classification_reason', sa.String(1000), nullable=False, server_default=''))
    with op.batch_alter_table('message_delivery') as batch:
        batch.add_column(sa.Column('attempt_id', sa.String(32)))
        batch.create_foreign_key('fk_delivery_attempt', 'orch_attempt', ['attempt_id'], ['id'])
        batch.create_index('ix_message_delivery_attempt_id', ['attempt_id'])
    with op.batch_alter_table('task_report') as batch:
        batch.add_column(sa.Column('attempt_id', sa.String(32)))
        batch.create_foreign_key('fk_report_attempt', 'orch_attempt', ['attempt_id'], ['id'])
        batch.create_index('ix_task_report_attempt_id', ['attempt_id'])
        batch.add_column(sa.Column('version', sa.Integer(), nullable=False, server_default='1'))
        batch.add_column(sa.Column('findings_md', sa.Text()))
        batch.create_unique_constraint('uq_task_report_attempt_version', ['attempt_id', 'version'])


def downgrade():
    with op.batch_alter_table('task_report') as batch:
        batch.drop_constraint('uq_task_report_attempt_version', type_='unique')
        batch.drop_index('ix_task_report_attempt_id')
        batch.drop_constraint('fk_report_attempt', type_='foreignkey')
        for name in ('attempt_id', 'version', 'findings_md'):
            batch.drop_column(name)
    with op.batch_alter_table('message_delivery') as batch:
        batch.drop_index('ix_message_delivery_attempt_id')
        batch.drop_constraint('fk_delivery_attempt', type_='foreignkey')
        batch.drop_column('attempt_id')
    with op.batch_alter_table('selection') as batch:
        batch.drop_index('ix_selection_assignment_id')
        batch.drop_constraint('fk_selection_assignment', type_='foreignkey')
        for name in ('assignment_id', 'catalog_revision', 'profile_digest', 'classification', 'classification_reason'):
            batch.drop_column(name)
    op.drop_table('orch_attempt')
    op.drop_table('orch_assignment')

    with op.batch_alter_table('team') as batch:
        batch.drop_column('protocol_version')
        batch.drop_column('attempt_counter')
