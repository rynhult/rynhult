"""Desktop composition only: real server/broker/fake providers in fixture-owned paths.

The fake broker runs as the test runner, never a production fallback. Actual
account separation is checked by config validation; privileged setup is dry-run.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from helpers import wait_for
from sqlalchemy import select

from acc.server.app import create_app
from acc.server.models import AuditLog, User

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Linux broker/account session composition only')

DESKTOP = Path(__file__).resolve().parents[1] / 'desktop'


def runtime():
    path = DESKTOP / 'execution.py'
    assert path.exists(), 'desktop execution composition is missing'
    spec = importlib.util.spec_from_file_location('desktop_execution', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_manifest_rejects_current_user_as_agent(tmp_path):
    module = runtime()
    import pwd
    user = pwd.getpwuid(os.getuid())
    with pytest.raises(ValueError, match='separate'):
        module.validate_account({'agent': user.pw_name, 'home': user.pw_dir}, os.getuid())


@pytest.fixture
def desktop_layout(env, monkeypatch):
    """Use production composition, including per-account socket/spool addressing."""
    from acc.server import broker_client
    from acc.server.config import Settings
    # The fixture socket belongs to this test process, never an installed account.
    monkeypatch.setattr(broker_client, 'uid_of', lambda account: os.getuid())
    monkeypatch.setattr(Settings, 'account_home', staticmethod(lambda account: env['home']))
    value = {'home': str(env['home']), 'projects': str(env['workspaces']),
             'run_dir': str(env['run_dir']), 'resources': str(DESKTOP.parent),
             'socket': str(env['run_dir'].parent / 'sockets' / f"{env['home'].name}.sock")}
    settings = runtime().execution_settings(env['settings'], value)
    settings.broker_socket_dir.mkdir()
    spool = settings.account_run_dir(settings.default_account)
    spool.mkdir()
    env['settings'] = settings
    env['broker'] = replace(env['broker'], run_dir=spool, socket_path=settings.broker_socket)
    return env


@pytest.fixture
def unprivileged_broker_tools(monkeypatch):
    """Model the agent's effective uid without changing real socket peer credentials.

    Only the broker module sees this stub. The fake npm still executes inside the
    fixture home, and production root refusal is exercised separately below.
    """
    from types import SimpleNamespace

    from acc.broker import server
    monkeypatch.setattr(server, 'os', SimpleNamespace(**{**vars(os), 'geteuid': lambda: 1001}))


@pytest.fixture
async def desktop_app(env, desktop_layout, broker, supervisor_threads):
    app = create_app(env['settings'])
    runtime().configure(app, provisioned=True)
    try:
        yield app
    finally:
        await app.state.ctx.broker.close()
        app.state.db.kw['bind'].dispose()


@pytest.mark.parametrize('provider', ['claude', 'codex'])
async def test_desktop_project_approval_session_terminal_and_files(env, desktop_app, provider):
    module = runtime()
    app = desktop_app
    install_fake_npm(env['home'])
    token, csrf = module.bootstrap_owner(app)
    # A second window/launch preserves one identity, without creating another owner.
    token, csrf = module.bootstrap_owner(app)
    with app.state.db() as db:
        assert [u.username for u in db.scalars(select(User))] == ['desktop-owner']
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url='http://acc',
                                cookies={'acc_session': token, 'acc_csrf': csrf},
                                headers={'x-acc-csrf': csrf, 'Origin': 'http://acc'}) as client:
        state = await client.get('/api/auth/state')
        assert state.json()['authenticated'] and not state.json()['setup_required']
        made = await client.post('/api/projects/new', json={'name': 'Desktop demo', 'source': 'empty'})
        assert made.status_code == 201, made.text
        project = made.json()
        body = {'project_id': project['id'], 'provider': provider, 'name': 'Desktop work'}
        blocked = await client.post('/api/sessions', json=body)
        assert blocked.status_code == 409 and 'Separate agent account' in blocked.text
        assert (await client.get('/api/sessions')).json() == []
        url = f"/api/desktop/projects/{project['id']}/native"
        assert (await client.post(url, json={'accept': True}, headers={'x-acc-csrf': 'bad'})).status_code == 403
        assert (await client.post(url, json={'accept': True})).status_code == 200
        launched = await client.post('/api/sessions', json=body)
        assert launched.status_code == 201, launched.text
        session = launched.json()
        changing = await client.post('/api/desktop/cli', json={'provider': 'codex', 'version': '0.163.0'})
        assert changing.status_code == 409 and 'Stop running sessions' in changing.text
        async def output():
            response = await client.get(f"/api/sessions/{session['id']}/terminal-output")
            return response.status_code == 200 and 'fake' in response.json()['text'].lower()
        await wait_for(output, timeout=15)
        assert session['run_as'] == env['settings'].default_account
        # Files use the real broker, not direct owner filesystem writes.
        file_path = Path(project['workspace_path']) / 'desktop-note.txt'
        file_path.write_text('persistent demo work')
        files = await app.state.ctx.broker.call('files', path=project['workspace_path'], action='read', rel='desktop-note.txt')
        assert files['content'] == 'persistent demo work'
        assert (await client.post('/api/sessions', json={**body, 'provider': 'copilot'})).status_code == 409
        assert (await client.post('/api/users/invites', json={})).status_code == 409
        assert (await client.post(f"/api/sessions/{session['id']}/stop")).status_code == 200
        with app.state.db() as db:
            assert db.scalar(select(AuditLog).where(AuditLog.action == 'desktop.native.approve')) is not None
    await app.state.ctx.broker.close()
    app.state.db.kw['bind'].dispose()


async def test_unprovisioned_desktop_fails_closed(env, broker):
    module = runtime()
    app = create_app(env['settings'])
    module.configure(app, provisioned=False)
    token, csrf = module.bootstrap_owner(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://acc',
                                cookies={'acc_session': token}, headers={'x-acc-csrf': csrf}) as client:
        status = await client.get('/api/desktop/status')
        assert status.json()['provisioned'] is False
        result = await client.post('/api/projects/new', json={'name': 'Blocked'})
        assert result.status_code == 409 and 'setup' in result.text.lower()
    await app.state.ctx.broker.close()
    app.state.db.kw['bind'].dispose()


def test_bootstrap_refuses_existing_server_people(env):
    module = runtime()
    app = create_app(env['settings'])
    with app.state.db() as db:
        db.add(User(username='Alex', role='owner', password_hash='', totp_secret=''))
        db.commit()
    with pytest.raises(ValueError, match='profile'):
        module.bootstrap_owner(app)
    app.state.db.kw['bind'].dispose()


async def test_native_approval_revocation_blocks_future_launches(env, desktop_app, monkeypatch):
    app = desktop_app
    module = runtime()
    token, csrf = module.bootstrap_owner(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://acc',
                                cookies={'acc_session': token}, headers={'x-acc-csrf': csrf}) as client:
        project = (await client.post('/api/projects/new', json={'name': 'Revocation demo'})).json()
        url = f"/api/desktop/projects/{project['id']}/native"
        original = app.state.ctx.broker.call
        identity_calls = []

        async def call(method, *args, **kwargs):
            if method == 'git_identity':
                identity_calls.append(kwargs)
            return await original(method, *args, **kwargs)

        monkeypatch.setattr(app.state.ctx.broker, 'call', call)
        assert (await client.post(url, json={'accept': True})).status_code == 200
        assert (await client.post(url, json={'accept': False})).status_code == 200
        refused = await client.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude', 'name': 'Blocked'})
        assert refused.status_code == 409
        assert identity_calls == []
        assert (await client.post(url, json={'accept': True})).status_code == 200
        from acc.server.models import Project
        with app.state.db() as db:
            db.get(Project, project['id']).workspace_path += '-moved'
            db.commit()
        moved = await client.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude', 'name': 'Moved'})
        assert moved.status_code == 409
        assert identity_calls == []


def test_desktop_broker_requires_socket_activation_and_clears_owner_environment(monkeypatch, tmp_path):
    from types import SimpleNamespace
    spec = importlib.util.spec_from_file_location('desktop_broker_entry', DESKTOP / 'broker.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    value = {'agent': 'rynd-1001', 'home': str(tmp_path / 'rynd-1001'),
             'run_dir': str(tmp_path / 'run'), 'projects': str(tmp_path / 'projects'),
             'socket': str(tmp_path / 'rynd-1001.sock')}
    monkeypatch.setattr(module, 'read_manifest', lambda uid: value)
    monkeypatch.setattr(module, 'validate_account', lambda value, uid: SimpleNamespace(pw_uid=55001))
    child_env = {'RYNHULT_DESKTOP_TOKEN': 'must-not-inherit', 'ACC_SESSION_ID': 'live-session', 'PATH': '/owner/bin', 'PYTHONPATH': '/owner/untrusted'}
    monkeypatch.setattr(module, 'os', SimpleNamespace(getuid=lambda: 55001, geteuid=lambda: 55001,
                                                    getpid=lambda: 999, environ=child_env))
    with pytest.raises(ValueError, match='systemd socket'):
        module.configuration(1001)
    child_env.update(LISTEN_PID='999', LISTEN_FDS='1')
    config = module.configuration(1001)
    settings = module.read_manifest(1001)
    from acc.server.config import Settings
    composed = runtime().execution_settings(Settings(), {**settings, 'resources': str(DESKTOP.parent)})
    assert config.run_dir == composed.account_run_dir(composed.default_account)
    assert config.allowed_uids == frozenset({1001})
    assert config.allowed_roots == (Path(value['projects']),)
    assert child_env['HOME'] == value['home']
    assert child_env['LANG'] == 'C.UTF-8'
    assert '/owner/bin' not in child_env['PATH']
    # The bundled interpreter has no editable install. PTY attach launches a fresh
    # `python -m acc.broker.pty_exec`, so test without site-packages as well.
    import subprocess
    child = subprocess.run([sys.executable, '-S', '-c',
                            'import acc.broker.pty_exec; print(acc.broker.pty_exec.__file__)'],
                           env=child_env, capture_output=True, text=True)
    assert child.returncode == 0, child.stderr
    assert Path(child.stdout.strip()).resolve() == (DESKTOP.parent / 'src/acc/broker/pty_exec.py').resolve()
    assert 'ACC_SESSION_ID' not in child_env and 'RYNHULT_DESKTOP_TOKEN' not in child_env
    assert config.providers['copilot'] == '/nonexistent/desktop-provider-unavailable'


@pytest.mark.parametrize('provider', ['claude', 'codex'])
def test_real_desktop_provider_when_test_account_is_available(provider):
    """Never borrow this worker's credentials to make a smoke test pass."""
    import pwd
    import shutil

    from acc import providers
    from acc.env import environment
    if environment.get('DESKTOP_REAL_CLI_ACCOUNT') != pwd.getpwuid(os.getuid()).pw_name:
        pytest.skip('No disposable real-CLI test account selected: set DESKTOP_REAL_CLI_ACCOUNT only inside that account')
    exe = shutil.which(provider)
    if not exe:
        pytest.skip(f'{provider} is not installed for the selected test account')
    adapter = providers.get(provider)
    health = adapter.auth_health(exe, Path.home())
    if not health.get('logged_in'):
        pytest.skip(f'{provider} is not logged in for the selected test account')
    # Real invocation is read-only: provider login qualification, no billable prompt.
    assert health['logged_in'] is True


def install_fake_npm(home):
    npm = home / '.local/bin/npm'
    npm.parent.mkdir(parents=True)
    npm.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
home = pathlib.Path(os.environ['HOME'])
state = home / 'fake-npm-installed.json'
if sys.argv[1] == 'install':
    name, version = sys.argv[-1].rsplit('@', 1)
    state.write_text(json.dumps({'dependencies': {name: {'version': version}}}))
else:
    print(state.read_text())
''')
    npm.chmod(0o755)


async def test_cli_install_is_unprivileged_pinned_and_provider_only(env, desktop_app, unprivileged_broker_tools):
    install_fake_npm(env['home'])
    app = desktop_app
    token, csrf = runtime().bootstrap_owner(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://acc',
                                cookies={'acc_session': token}, headers={'x-acc-csrf': csrf}) as client:
        invalid = await client.post('/api/desktop/cli', json={'provider': 'bash', 'version': '1.2.3'})
        assert invalid.status_code == 422
        pinned = await client.post('/api/desktop/cli', json={'provider': 'codex', 'version': '0.163.0'})
        assert pinned.status_code == 200, pinned.text
        assert pinned.json()['version'] == '0.163.0'
        import json
        assert json.loads((env['home'] / 'fake-npm-installed.json').read_text()) == {
            'dependencies': {'@openai/codex': {'version': '0.163.0'}}}


async def test_cli_install_refuses_root_without_running_npm(env, desktop_app, monkeypatch):
    from types import SimpleNamespace

    from acc.broker import server
    monkeypatch.setattr(server, 'os', SimpleNamespace(**{**vars(os), 'geteuid': lambda: 0}))
    install_fake_npm(env['home'])
    token, csrf = runtime().bootstrap_owner(desktop_app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=desktop_app), base_url='http://acc',
                                cookies={'acc_session': token}, headers={'x-acc-csrf': csrf}) as client:
        response = await client.post('/api/desktop/cli', json={'provider': 'codex', 'version': '0.163.0'})
    assert response.status_code == 502
    assert 'unprivileged agent account, never root' in response.text
    assert not (env['home'] / 'fake-npm-installed.json').exists()


def test_non_linux_desktop_does_not_read_linux_account_configuration(monkeypatch):
    from types import SimpleNamespace
    module = runtime()
    monkeypatch.setattr(module, 'sys', SimpleNamespace(platform='win32'), raising=False)
    monkeypatch.setattr(module, 'Path', lambda value: pytest.fail('Windows must not inspect Linux execution paths'))
    assert module.read_manifest(1001) is None


async def test_desktop_refuses_general_tool_and_package_changes(env, broker):
    """The tool request service calls the broker through a fresh client, bypassing the desktop's tool_change
    refusal: only the fixed Claude/Codex installer may change software in the agent account (review 2026-10-25)."""
    module = runtime()
    app = create_app(env['settings'])
    module.configure(app, provisioned=True)
    token, csrf = module.bootstrap_owner(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://acc',
                                cookies={'acc_session': token}, headers={'x-acc-csrf': csrf}) as client:
        for method, path, body in [('GET', '/api/tools', None),
                                   ('POST', '/api/tools/requests', {'manager': 'npm', 'name': 'left-pad', 'version': '1.3.0'}),
                                   ('POST', '/api/tools/requests/1/approve', {}),
                                   ('GET', '/api/packages', None),
                                   ('POST', '/api/packages/claude/install', {})]:
            response = await client.request(method, path, json=body)
            assert response.status_code == 409 and 'Unavailable' in response.text, (path, response.status_code)
    await app.state.ctx.broker.close()
    app.state.db.kw['bind'].dispose()


def test_break_it_6_desktop_routes_have_explicit_authority_inventory(desktop_app):
    import json

    from fastapi.routing import APIRoute
    from test_ownership import all_routes

    records = json.loads((DESKTOP.parent / 'tests/ownership_inventory.json').read_text())
    classified = {(r['method'], r['path']) for r in records if r.get('configuration') == 'desktop'}
    actual = {(method, prefix + route.path) for prefix, route in all_routes(desktop_app.routes)
              if isinstance(route, APIRoute) and route.path.startswith('/api/desktop/') for method in route.methods}
    assert actual == classified


async def test_break_it_6_desktop_routes_refuse_foreign_authority(desktop_app):
    from acc.server import auth

    runtime().bootstrap_owner(desktop_app)
    with desktop_app.state.db() as db:
        member = User(username='b6-member', role='member', account='member-agent', password_hash='', totp_secret='')
        db.add(member)
        db.flush()
        token, csrf = auth.create_session(db, member, 'test')
        db.commit()
    endpoints = [('GET', '/api/desktop/status', None),
                 ('POST', '/api/desktop/cli', {'provider': 'codex', 'version': '0.163.0'}),
                 ('POST', '/api/desktop/projects/123/native', {'accept': True})]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=desktop_app), base_url='http://acc') as client:
        for method, path, body in endpoints:
            assert (await client.request(method, path, json=body)).status_code == 401
            # A session/tool/provider capability is not an installation owner bearer.
            assert (await client.request(method, path, json=body,
                headers={'Authorization': 'Bearer fixture-foreign-capability'})).status_code == 401
        client.cookies.set('acc_session', token)
        client.headers.update({'x-acc-csrf': csrf, 'Origin': 'http://acc'})
        for method, path, body in endpoints[1:]:
            assert (await client.request(method, path, json=body)).status_code == 403


async def test_break_it_6_cli_update_waits_for_actual_session_exit(env, desktop_app, unprivileged_broker_tools, monkeypatch):
    import asyncio

    install_fake_npm(env['home'])
    app = desktop_app
    token, csrf = runtime().bootstrap_owner(app)
    entered, release = asyncio.Event(), asyncio.Event()
    call = app.state.ctx.broker.call
    async def delayed_stop(method, *args, **kwargs):
        if method == 'kill':
            entered.set()
            await release.wait()
        return await call(method, *args, **kwargs)
    monkeypatch.setattr(app.state.ctx.broker, 'call', delayed_stop)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://acc',
                                cookies={'acc_session': token}, headers={'x-acc-csrf': csrf}) as client:
        project = (await client.post('/api/projects/new', json={'name': 'Stop race'})).json()
        assert (await client.post(f"/api/desktop/projects/{project['id']}/native", json={'accept': True})).status_code == 200
        launched = await client.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude', 'name': 'Still alive'})
        assert launched.status_code == 201, launched.text
        stopping = asyncio.create_task(client.post(f"/api/sessions/{launched.json()['id']}/stop"))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            # Stop intent is durable, but its real tmux pane is deliberately still alive.
            update = await client.post('/api/desktop/cli', json={'provider': 'codex', 'version': '0.163.0'})
        finally:
            release.set()
            assert (await stopping).status_code == 200
        assert update.status_code == 409, update.text
        assert not (env['home'] / 'fake-npm-installed.json').exists()
        retry = await client.post('/api/desktop/cli', json={'provider': 'codex', 'version': '0.163.0'})
        assert retry.status_code == 200, retry.text
