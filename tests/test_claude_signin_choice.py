"""Billing choice is enforced at delivery and survives all session relaunches."""
import json
from datetime import datetime
from types import SimpleNamespace

import pytest
from alembic import command
from helpers import wait_for
from sqlalchemy import inspect, text
from test_schedules import person  # noqa: F401
from test_supervisor import get, state_is
from test_vault import CANARY, OTHER, add, session_env

from acc.broker import claude_auth as broker_auth
from acc.providers.claude_auth import AUTH_ENV, CODE
from acc.server.api.schemas import SessionIn
from acc.server.db import alembic_config, make_engine
from acc.server.vault import Plan, VaultError

pytestmark = pytest.mark.claude_launch_auth


@pytest.mark.parametrize('variable', sorted(AUTH_ENV))
def test_filter_only_claude_auth(variable):
    auth = SimpleNamespace(id='auth', variable=variable)
    other = SimpleNamespace(id='other', variable='OPENAI_API_KEY')
    plan = Plan([auth, other], [auth, other], {'search': {'TOKEN': auth, 'OTHER': other}})
    filtered = plan.claude_auth('claude')
    assert filtered.held_back == [other]
    assert filtered.mcp_refs == {'search': {'OTHER': other}}
    assert auth.id not in filtered.ids
    assert plan.claude_auth('claude').keys == [other]
    assert plan.claude_auth('claude', 'api_key').keys == [auth, other]
    assert plan.claude_auth('codex').keys == [auth, other]
    assert plan.keys == [auth, other]


def test_api_choice_requires_grant_and_is_not_a_cli_option():
    with pytest.raises(VaultError, match='requires an Anthropic'):
        Plan([], []).claude_auth('claude', 'api_key')
    body = SessionIn(provider='claude', name='example', claude_auth='api_key')
    assert 'claude_auth' not in body.inputs().as_dict()
    assert SessionIn(provider='claude', name='example').claude_auth == 'subscription'
    with pytest.raises(ValueError):
        SessionIn(provider='claude', name='example', claude_auth='other')


@pytest.mark.parametrize('choice', ['subscription', 'api_key'])
async def test_launch_and_resume_keep_choice(api, env, project, monkeypatch, choice, caplog):
    # Model Claude's documented status from the actual supplied launch environment.
    monkeypatch.setattr(broker_auth, 'probe', lambda exe, cwd, env, args, **kw:
                        'api_key' if env.get('ANTHROPIC_API_KEY') else 'subscription')
    anthropic = await add(api, 'Anthropic work', 'ANTHROPIC_API_KEY')
    other = await add(api, 'Other work', 'OPENAI_API_KEY', OTHER)
    for key in (anthropic, other):
        assert (await api.post(f"/api/vault/keys/{key['id']}/give", json={'project_id': project['id']})).status_code == 200
    body = {'project_id': project['id'], 'provider': 'claude', 'name': 'billing'}
    if choice == 'api_key':
        body['claude_auth'] = choice
    preview_response = await api.post('/api/sessions/preview', json=body)
    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()
    assert [k['name'] for k in preview['claude_auth_keys']] == ['Anthropic work']
    names = {k['variable'] for k in preview['keys']['keys']}
    assert names == ({'OPENAI_API_KEY', 'ANTHROPIC_API_KEY'} if choice == 'api_key' else {'OPENAI_API_KEY'})
    response = await api.post('/api/sessions', json=body)
    assert response.status_code == 201, response.text
    session = response.json()
    assert session['claude_auth'] == choice
    await wait_for(state_is(api, session['id'], 'idle'))
    for operation in (None, 'resume', 'broker_restart'):
        if operation is not None:
            await api.post(f"/api/sessions/{session['id']}/stop")
            await wait_for(state_is(api, session['id'], 'stopped'))
        if operation:
            if operation == 'broker_restart':
                env['broker'].auth_contexts.clear()
            result = await api.post(f"/api/sessions/{session['id']}/resume")
            assert result.status_code == 200, result.text
            await wait_for(state_is(api, session['id'], 'idle'))
        written = session_env(env, session['id'])
        assert f'export OPENAI_API_KEY={OTHER}\n' in written
        assert ('export ANTHROPIC_API_KEY=' in written) == (choice == 'api_key')
        detail = await get(api, session['id'])
        assert detail['claude_auth'] == choice
        assert detail['auth']['mode'] == choice
        message = await api.post(f"/api/sessions/{session['id']}/message", json={'text': 'Automated followup'})
        assert message.status_code == (200 if choice == 'api_key' else 409), message.text
        if choice == 'subscription':
            assert message.json()['detail']['code'] == CODE
        for value in (CANARY, OTHER):
            assert value not in json.dumps(detail)
            assert value not in preview_response.text
            assert value not in response.text
            assert value not in caplog.text


async def test_api_choice_without_grant_is_rejected(api, project):
    body = {'project_id': project['id'], 'provider': 'claude', 'name': 'billing', 'claude_auth': 'api_key'}
    for path in ('/api/sessions/preview', '/api/sessions'):
        response = await api.post(path, json=body)
        assert response.status_code == 422, response.text
        assert 'requires an Anthropic' in response.text


def test_migration_up_down(tmp_path):
    url = f'sqlite:///{tmp_path}/migration.db'
    config = alembic_config(url)
    command.upgrade(config, '0086')
    engine = make_engine(url)
    # Minimal rows exercise backfill without coupling to current ORM defaults.
    with engine.begin() as db:
        db.execute(text("INSERT INTO vault_key (id,name,variable,ciphertext,owner,shared,remote,created_at,updated_at) "
                        "VALUES ('auth','Auth','ANTHROPIC_API_KEY','sealed','person',0,0,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP), "
                        "('other','Other','OPENAI_API_KEY','sealed','person',0,0,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        # Reflect defaults/nullable columns through the existing table for a realistic old row.
        from sqlalchemy import MetaData, Table

        from acc.server.models import LaunchSpec, Project
        now = datetime(2026, 11, 4)
        db.execute(LaunchSpec.__table__.insert().values(id=1, provider='claude', inputs={}, files={}, digest='fixture', created_at=now))
        schedules = Table('schedule', MetaData(), autoload_with=db)
        grants = Table('vault_grant', MetaData(), autoload_with=db)
        grants_data = dict(key_id='auth', project_id=1, given_by='person', server_only=False, given_at=now)
        db.execute(Project.__table__.insert().values(id=1, slug='example', name='Example',
                                                    host='local', workspace_path='/tmp/example', account='agent'))
        db.execute(grants.insert().values(**grants_data))
        schedule_launches = {
            'old': {'project_id': 1, 'provider': 'claude'},
            'explicit': {'project_id': 1, 'provider': 'claude', 'claude_auth': 'subscription'},
            'codex': {'project_id': 1, 'provider': 'codex'},
            'ungranted': {'project_id': 2, 'provider': 'claude'},
        }
        for ident, launch in schedule_launches.items():
            db.execute(schedules.insert().values(id=ident, owner='person', account='agent', launch=launch,
                       timing={}, timezone='UTC', missed='once', enabled=False, deleted=False, created_at=now))
        sessions = Table('session', MetaData(), autoload_with=db)
        for ident, keys in [('api', ['auth']), ('subscription', ['other']), ('none', [])]:
            db.execute(sessions.insert().values(id=ident, provider='claude', name=ident, tmux_name=f'acc-{ident}',
                       desired_state='stopped', observed_state='stopped', created_at=now, updated_at=now,
                       launch_spec_id=1, run_as='agent', multirun_stop_pending=False, team_stop_pending=False,
                       team_depth=0, recovery_attempts=0, spool_offset=0, vault_keys=keys))
    command.upgrade(config, '0087')
    with engine.connect() as db:
        assert dict(db.execute(text('SELECT id,claude_auth FROM session')).all()) == {
            'api': 'api_key', 'subscription': 'subscription', 'none': 'subscription'}
        migrated = {ident: json.loads(launch) for ident, launch in
                    db.execute(text('SELECT id,launch FROM schedule'))}
        assert migrated == {**schedule_launches, 'old': {**schedule_launches['old'], 'claude_auth': 'api_key'}}
    command.downgrade(config, '0086')
    assert 'claude_auth' not in {c['name'] for c in inspect(engine).get_columns('session')}
    command.upgrade(config, '0087')
    assert 'claude_auth' in {c['name'] for c in inspect(engine).get_columns('session')}
    engine.dispose()


@pytest.mark.parametrize('choice', ['subscription', 'api_key'])
async def test_schedules_require_explicit_api_choice(api, project, person, monkeypatch, choice):  # noqa: F811
    from test_schedules import payload
    monkeypatch.setattr(broker_auth, 'probe', lambda exe, cwd, env, args, **kw:
                        'api_key' if env.get('ANTHROPIC_API_KEY') else 'subscription')
    key = await add(api, 'Schedule Claude', 'ANTHROPIC_API_KEY')
    await api.post(f"/api/vault/keys/{key['id']}/give", json={'project_id': project['id']})
    body = payload(project)
    if choice == 'api_key':
        body['launch']['claude_auth'] = choice
    made = await api.post('/api/schedules', json=body)
    assert made.status_code == 201, made.text
    ident = made.json()['id']
    assert made.json()['launch']['claude_auth'] == choice
    body['launch']['claude_auth'] = 'subscription' if choice == 'api_key' else 'api_key'
    edited = await api.put(f'/api/schedules/{ident}', json=body)
    assert edited.status_code == 200, edited.text
    assert edited.json()['launch']['claude_auth'] == body['launch']['claude_auth']
    body['launch']['claude_auth'] = choice
    assert (await api.put(f'/api/schedules/{ident}', json=body)).status_code == 200
    assert (await api.post(f'/api/schedules/{ident}/run')).status_code == 201
    async def settled():
        schedule = next(row for row in (await api.get('/api/schedules')).json() if row['id'] == ident)
        run = schedule['runs'][0]
        return run if run['status'] == ('running' if choice == 'api_key' else 'skipped') else None
    run = await wait_for(settled)
    detail = await get(api, run['session_id'])
    assert detail['claude_auth'] == choice
    if choice == 'subscription':
        assert not detail['runs']
    await api.post(f'/api/schedules/{ident}/cancel')


async def test_helpers_require_explicit_api_choice(api, project, env, monkeypatch):
    from test_changes import repo
    from test_team import grant, request
    monkeypatch.setattr(broker_auth, 'probe', lambda exe, cwd, env, args, **kw:
                        'api_key' if env.get('ANTHROPIC_API_KEY') else 'subscription')
    repo(env['workspaces'] / 'demo')
    key = await add(api, 'Helper Claude', 'ANTHROPIC_API_KEY')
    await api.post(f"/api/vault/keys/{key['id']}/give", json={'project_id': project['id']})
    made = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude',
                          'name': 'Lead', 'worktree': True, 'orchestrator': grant(), 'claude_auth': 'api_key'})
    assert made.status_code == 201, made.text
    lead = made.json()
    await wait_for(state_is(api, lead['id'], 'idle'))
    default = await request(api, env, lead, 'start', name='Subscription helper', agent='claude', task='Example work')
    assert not default['ok'], default
    explicit = await request(api, env, lead, 'start', name='API helper', agent='claude', task='Example work', claude_auth='api_key')
    assert explicit['ok'], explicit
    assert explicit['result']['claude_auth'] == 'api_key'


@pytest.mark.parametrize('variable', ['ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_FOUNDRY_API_KEY'])
def test_container_adapter_keeps_explicit_api_credentials(variable):
    from acc.container_credentials import _narrow
    assert _narrow('claude', {'launch_env': {variable: 'example-token'}}) == {
        'launch_env': {variable: 'example-token'}}


def test_auth_probe_removes_ambient_overrides(monkeypatch):
    import subprocess
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'ambient-example')
    monkeypatch.setenv('ANTHROPIC_AUTH_TOKEN', 'ambient-example')
    def run(argv, **kwargs):
        assert not AUTH_ENV.intersection(kwargs['env'])
        return subprocess.CompletedProcess(argv, 0, '{"loggedIn":true,"authMethod":"claude.ai"}')
    monkeypatch.setattr(broker_auth.subprocess, 'run', run)
    assert broker_auth.probe('claude', '/tmp', {}, []) == 'subscription'


@pytest.mark.parametrize('choice', ['subscription', 'api_key'])
async def test_mcp_auth_refs_filtered_at_delivery(api, env, project, choice):
    from acc.server import vault
    key = await add(api, 'MCP Anthropic', 'ANTHROPIC_API_KEY')
    await api.post(f"/api/vault/keys/{key['id']}/give", json={'project_id': project['id']})
    added = await api.post('/api/catalog/mcps', json={'id': 'auth-search', 'command': 'true',
                           'env': {'SEARCH_TOKEN': vault.ref(key['id']), 'PLAIN': 'example'}, 'providers': ['claude']})
    assert added.status_code == 200, added.text
    supervisor = api.app.state.supervisor
    with supervisor.db() as db:
        delivery = supervisor.vault.deliver(db, project['id'], '@api', False, supervisor.catalog,
                                            ('auth-search',), provider='claude', claude_auth=choice)
    selected_env = delivery.catalog.mcps['auth-search'].meta['env']
    assert selected_env == ({'SEARCH_TOKEN': CANARY, 'PLAIN': 'example'} if choice == 'api_key'
                            else {'PLAIN': 'example'})
    assert (key['id'] in delivery.plan.ids) == (choice == 'api_key')
