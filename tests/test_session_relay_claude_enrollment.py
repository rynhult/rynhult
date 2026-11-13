"""Dormant Claude opt-in through actual API/supervisor/private broker, fake CLI."""
from pathlib import Path

import pytest
from test_auth import cookie_client, do_setup
from test_session_relay_ownership import ownership_case  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc import providers
from acc.server.models import RelayOwnership, RelayRun, RelaySession, Session, SessionRun
from acc.session_relay import protocol as p


async def person(api, env):
    await do_setup(api, env)
    client = cookie_client(api)
    return client, {'Origin': 'http://acc', 'X-ACC-CSRF': client.cookies.get('acc_csrf')}


def body(project, **extra):
    return {'project_id': project['id'], 'provider': 'claude', 'name': 'Personal Claude relay',
        'claude_auth': 'api_key', 'session_relay': True, 'worktree': False, **extra}


async def test_claude_enrollment_stays_off_even_with_feature_on(api, project, env, monkeypatch):
    monkeypatch.setenv('ACC_FEATURE_SESSION_RELAY', 'true')
    client, headers = await person(api, env)
    for route in ('/api/sessions/preview', '/api/sessions'):
        reply = await client.post(route, json=body(project), headers=headers)
        assert reply.status_code == 409, reply.text
    with api.app.state.db() as db:
        assert not db.query(RelaySession).count()


@pytest.mark.parametrize('extra', [{'initial_prompt': 'no seeded input'}, {'automation': {'goal': 'no automated launch'}},
    {'permission_mode': 'bypassPermissions'}, {'remote_control': True}, {'claude_auth': 'subscription'}])
async def test_claude_opt_in_refuses_unqualified_input_config_and_unapproved_subscription(api, project, env, monkeypatch, extra):
    monkeypatch.setenv('ACC_FEATURE_SESSION_RELAY', 'true')
    monkeypatch.setattr('acc.server.session_relay.claude_public.PUBLIC_CLAUDE_QUALIFIED', True)
    monkeypatch.setattr('acc.server.session_relay.exclusive_public.CLAUDE_LAUNCH_QUALIFIED', True)
    client, headers = await person(api, env)
    reply = await client.post('/api/sessions', json=body(project, **extra), headers=headers)
    assert reply.status_code == 409, reply.text
    with api.app.state.db() as db:
        assert not db.query(RelaySession).count()


@pytest.mark.parametrize('auth_mode', ['api_key', 'subscription'])
async def test_person_claude_opt_in_enrolls_l1_without_input_or_capability_promotion(api, project, env, ownership_case, monkeypatch, auth_mode):  # noqa: F811
    import acc.broker.claude_owner as module
    monkeypatch.setenv('ACC_FEATURE_SESSION_RELAY', 'true')
    monkeypatch.setattr('acc.server.session_relay.claude_public.PUBLIC_CLAUDE_QUALIFIED', True)
    monkeypatch.setattr('acc.server.session_relay.exclusive_public.CLAUDE_LAUNCH_QUALIFIED', True)
    monkeypatch.setattr(module, 'CLI_DIGEST', p.sha256_digest(Path(env['broker'].providers['claude']).read_bytes()))
    started = []
    factory = module.ClaudeOwnerFactory
    class RecordingFactory(factory):
        def __init__(self, *args):
            super().__init__(*args)
            started.append(self)
    monkeypatch.setattr(module, 'ClaudeOwnerFactory', RecordingFactory)
    # Fake CLI's own wire ID, never a production substitute or first-message seed.
    monkeypatch.setattr(providers.get('claude'), 'new_native_id', lambda: 'fixture-conversation')
    client, headers = await person(api, env)
    client.headers.update(headers)
    if auth_mode == 'api_key':
        from test_vault import add
        credential = await add(client, 'Fixture Anthropic', 'ANTHROPIC_API_KEY')
        given = await client.post(f"/api/vault/keys/{credential['id']}/give", json={'project_id': project['id']})
        assert given.status_code == 200, given.text
    else:
        from acc.server.claude_subscription import VERSION
        monkeypatch.setenv('ACC_FEATURE_CLAUDE_SUBSCRIPTION_CHAT', 'true')
        approved = await client.put('/api/preferences/claude-subscription', json={'enabled': True, 'version': VERSION})
        assert approved.status_code == 200, approved.text
    preview = await client.post('/api/sessions/preview', json=body(project, claude_auth=auth_mode), headers=headers)
    assert preview.status_code == 200, preview.text
    command = preview.json()['command']
    offset = command.index('--output-format')
    assert command[offset:offset + 3] == ['--output-format', 'stream-json', '--verbose']
    if auth_mode == 'subscription':
        from acc.providers.claude_auth import AUTH_ENV
        assert command[0] == 'env'
        assert all(command[command.index(variable) - 1] == '-u' for variable in AUTH_ENV)
    assert command[command.index('--permission-prompt-tool') + 1] == 'stdio'
    assert command[command.index('--session-id') + 1] == '<native-id>'
    assert '--resume' not in command
    assert not api.app.state.ctx.relay_runtime.runs if hasattr(api.app.state.ctx, 'relay_runtime') else True
    created = await client.post('/api/sessions', json=body(project, claude_auth=auth_mode), headers=headers)
    assert created.status_code == 201, created.text
    identifier = created.json()['id']
    with api.app.state.db() as db:
        row = db.get(RelaySession, identifier)
        session = db.get(Session, identifier)
        run = db.get(RelayRun, row.current_run_id)
        assert session.native_id == 'fixture-conversation'
        assert not row.sealed and row.capabilities == {}
        assert run.frozen_policy['ownershipMode'] == 'exclusive_idle'
        assert run.frozen_policy['authMode'] == auth_mode
        assert db.get(SessionRun, run.run_id).verified_at
        assert db.get(RelayOwnership, identifier).view == 'chat'
    runtime = api.app.state.ctx.relay_runtime.runs[identifier]
    assert len(started) == 1
    assert started[0].spec.spec['cli_path'] == str(Path(env['broker'].providers['claude']).resolve())
    broker_state = started[0].runtime
    assert not broker_state.owners.current.active
    assert not runtime.grants
    if auth_mode == 'subscription':
        revoked = await client.put('/api/preferences/claude-subscription', json={'enabled': False})
        assert revoked.status_code == 200
        with pytest.raises(ConnectionError):
            await broker_state.owner_authorized()
        assert not runtime.grants
    stopped = await client.post(f'/api/sessions/{identifier}/stop', headers=headers)
    assert stopped.status_code == 200, stopped.text
    assert broker_state.owners.current.process.returncode is not None


@pytest.mark.parametrize('failure', [None, 'digest', 'sdk', 'tmux', 'socat', 'bwrap', 'provider'])
def test_claude_installation_probe_is_read_only_and_fail_closed(tmp_path, monkeypatch, failure):
    import importlib.metadata
    import shutil
    from types import SimpleNamespace

    from acc.broker import claude_owner
    from acc.broker.relay_runtime import RuntimeTransportFactory
    executable = tmp_path / 'fake-claude-pin'
    executable.write_bytes(b'fixture bytes; never executed')
    executable.chmod(0o700)
    monkeypatch.setattr(claude_owner, 'CLI_DIGEST', p.sha256_digest(b'wrong' if failure == 'digest' else executable.read_bytes()))
    monkeypatch.setattr(importlib.metadata, 'version', lambda _: 'wrong' if failure == 'sdk' else '0.1.50')
    monkeypatch.setattr(shutil, 'which', lambda name: None if name == failure else
        str(executable) if name == str(executable) else '/fixture/' + name)
    original = Path.is_file
    monkeypatch.setattr(Path, 'is_file', lambda path: failure != 'bwrap' if str(path) == '/usr/bin/bwrap' else
        True if str(path) == '/proc/self/stat' else original(path))
    factory = RuntimeTransportFactory(SimpleNamespace(providers={'claude': str(executable)}))
    expected = {'available': failure is None}
    if failure != 'provider':
        expected.update(provider='claude', cliDigest=claude_owner.CLI_DIGEST)
    assert factory.probe('unknown' if failure == 'provider' else 'claude') == expected
    assert not factory.active
