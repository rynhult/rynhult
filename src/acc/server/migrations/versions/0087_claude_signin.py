"""Persist Claude billing choice; existing sessions retain their API delivery."""
import sqlalchemy as sa
from alembic import op

revision = '0087'
down_revision = '0086'
branch_labels = depends_on = None


def upgrade():
    op.add_column('session', sa.Column('claude_auth', sa.String(16), nullable=False, server_default='subscription'))
    # Existing launches delivered granted keys. Preserve that choice on resume.
    connection = op.get_bind()
    sessions = sa.table('session', sa.column('id', sa.String()), sa.column('provider', sa.String()),
                        sa.column('vault_keys', sa.JSON()), sa.column('claude_auth', sa.String()))
    keys = sa.table('vault_key', sa.column('id', sa.String()), sa.column('variable', sa.String()))
    auth_variables = {
        'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'CLAUDE_CODE_OAUTH_TOKEN',
        'ANTHROPIC_FOUNDRY_API_KEY', 'CLAUDE_CODE_USE_BEDROCK',
        'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY',
    }
    auth_ids = set(connection.scalars(sa.select(keys.c.id).where(keys.c.variable.in_(auth_variables))))
    retired = sa.table('vault_retired', sa.column('key_id', sa.String()), sa.column('variable', sa.String()))
    auth_ids.update(connection.scalars(sa.select(retired.c.key_id).where(retired.c.variable.in_(auth_variables))))
    for session_id, delivered in connection.execute(sa.select(sessions.c.id, sessions.c.vault_keys)
                                                    .where(sessions.c.provider == 'claude')):
        if auth_ids.intersection(delivered or []):
            connection.execute(sessions.update().where(sessions.c.id == session_id).values(claude_auth='api_key'))

    schedules = sa.table('schedule', sa.column('id', sa.String()), sa.column('launch', sa.JSON()))
    grants = sa.table('vault_grant', sa.column('key_id', sa.String()), sa.column('project_id', sa.Integer()),
                      sa.column('server_only', sa.Boolean()))
    projects = set(connection.scalars(sa.select(grants.c.project_id).join(keys, keys.c.id == grants.c.key_id)
                                     .where(keys.c.variable.in_(auth_variables), grants.c.server_only.is_(False))))
    for schedule_id, launch in connection.execute(sa.select(schedules.c.id, schedules.c.launch)):
        if (launch.get('provider', 'claude') == 'claude' and 'claude_auth' not in launch
                and launch.get('project_id') in projects):
            connection.execute(schedules.update().where(schedules.c.id == schedule_id)
                               .values(launch={**launch, 'claude_auth': 'api_key'}))


def downgrade():
    op.drop_column('session', 'claude_auth')
