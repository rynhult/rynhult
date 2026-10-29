"""Mac coordinator probe on a fixture-owned broker; no live desktop connection."""
import importlib.util
import os
import pwd
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from acc import providers
from acc.server.broker_client import BrokerClient

ROOT = Path(__file__).resolve().parents[1]


def probe_module():
    source = ROOT/'desktop/tests/mac_phase1_probe.py'
    assert source.exists(), 'coordinator broker probe missing'
    spec = importlib.util.spec_from_file_location('mac_coordinator_probe', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def probe_layout(env):
    agent = pwd.getpwuid(os.getuid()).pw_name
    spool = env['run_dir']/agent
    spool.mkdir()
    cli = env['home']/'fake-codex-version'
    cli.write_text('#!/bin/sh\nif [ "$1" = login ]; then echo "Logged in with test account"; else echo MAC_FAKE_CODEX; fi\n')
    cli.chmod(0o755)
    env['broker'] = replace(env['broker'], run_dir=spool,
        providers={**{name: '/nonexistent' for name in providers.PROVIDERS}, 'codex': str(cli), 'gh': '/nonexistent'})
    return dict(agent=agent, home=str(env['home']), run_dir=str(env['run_dir']),
                projects=str(env['workspaces']), socket=str(env['broker'].socket_path), agent_uid=os.getuid())


async def test_coordinator_probe_checks_files_and_logged_in_provider_pty(env, probe_layout, broker):
    module = probe_module()
    client = BrokerClient(env['broker'].socket_path, trusted_uids=frozenset({os.getuid()}))
    try:
        report = await module.probe(probe_layout, client)
        assert report['broker_account'] == probe_layout['agent']
        assert report['files'] == 'passed'
        assert report['providers']['claude']['skipped'] == 'CLI not installed for the agent account'
        assert report['providers']['codex']['terminal'] == 'passed'
        assert not list((env['workspaces']/'local').glob('desktop-mac-smoke-*'))
    finally:
        await client.close()


async def test_mac_setup_status_selects_installer_instructions(env, broker, monkeypatch):
    from types import SimpleNamespace

    import httpx
    from test_desktop_sessions import runtime

    from acc.server.app import create_app
    module = runtime()
    monkeypatch.setattr(module,'sys',SimpleNamespace(platform='darwin'))
    app = create_app(env['settings'])
    module.configure(app,provisioned=False)
    token, csrf = module.bootstrap_owner(app)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://acc',
            cookies={'acc_session':token},headers={'x-acc-csrf':csrf}) as client:
            status = (await client.get('/api/desktop/status')).json()
            assert status['setup_supported'] is True
            assert status['setup_kind'] == 'macos-installer'
            assert status['provisioned'] is False
    finally:
        await app.state.ctx.broker.close()
        app.state.db.kw['bind'].dispose()


async def test_recreated_mac_agent_requires_fresh_project_consent(env, broker):
    import httpx
    from test_desktop_sessions import runtime

    from acc.server.app import create_app
    module = runtime()
    app = create_app(env['settings'])
    app.state.desktop_agent_identity = 'original-agent-guid'
    module.configure(app,provisioned=True)
    token, csrf = module.bootstrap_owner(app)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://acc',
            cookies={'acc_session':token},headers={'x-acc-csrf':csrf}) as client:
            project = (await client.post('/api/projects/new',json={'name':'Mac consent demo'})).json()
            consent = f"/api/desktop/projects/{project['id']}/native"
            assert (await client.post(consent,json={'accept':True})).status_code == 200
            assert (await client.get('/api/desktop/status')).json()['projects'][0]['approved'] is True
            # A fresh installer receipt after purge/recreate is a new principal, even at the same path/name.
            app.state.desktop_agent_identity = 'replacement-agent-guid'
            assert (await client.get('/api/desktop/status')).json()['projects'][0]['approved'] is False
            refused = await client.post('/api/sessions',json={'project_id':project['id'],'provider':'codex','name':'Blocked'})
            assert refused.status_code == 409
    finally:
        await app.state.ctx.broker.close()
        app.state.db.kw['bind'].dispose()


async def test_mac_activated_server_runs_the_fixture_provider_end_to_end(env, probe_layout):
    """Actual adapter/tmux/PTY on Linux; only launchd and distinct UIDs are simulated."""
    import asyncio
    import socket

    source = ROOT / 'desktop/mac_broker.py'
    spec = importlib.util.spec_from_file_location('mac_activated_fixture', source)
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(env['broker'].socket_path))
    listener.listen(8)
    task = asyncio.create_task(adapter.serve(env['broker'], listener))
    client = BrokerClient(env['broker'].socket_path, trusted_uids=frozenset({os.getuid()}))
    try:
        report = await probe_module().probe(probe_layout, client)
        assert report['files'] == 'passed'
        assert report['providers']['codex']['terminal'] == 'passed'
        assert adapter.ops.tmux(env['broker'], 'show-options', '-gv', 'default-shell', check=True).stdout.strip() == '/bin/sh'
        assert not list((env['workspaces']/'local').glob('desktop-mac-smoke-*'))
    finally:
        await client.close()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        listener.close()


@pytest.mark.parametrize('denial,token,error', [
    ('# note\nDenyUsers rynd-501\n', 'Secure token is DISABLED for user rynd-501\n', None),
    (None, 'Secure token is DISABLED for user rynd-501\n', 'Remote Login'),
    ('DenyUsers someone-else\n', 'Secure token is DISABLED for user rynd-501\n', 'Remote Login'),
    ('DenyUsers rynd-501\n', 'Secure token is ENABLED for user rynd-501\n', 'Secure Token'),
    ('DenyUsers rynd-501\n', 'something else\n', 'Secure Token')])
def test_probe_checks_remote_login_denial_and_no_secure_token(tmp_path, denial, token, error):
    module = probe_module()
    path = tmp_path/'100-rynhult-desktop-501.conf'
    if denial is not None:
        path.write_text(denial)
    run = lambda argv, **kw: SimpleNamespace(returncode=0, stdout='', stderr=token)  # sysadminctl reports on stderr
    info = {'agent': 'rynd-501', 'owner_uid': 501}
    if error is None:
        assert module.account_checks(info, ssh_denial=path, run=run, owner_uid=os.stat(path).st_uid) == {
            'remote_login': 'denied', 'secure_token': 'disabled'}
    else:
        with pytest.raises(ValueError, match=error):
            module.account_checks(info, ssh_denial=path, run=run, owner_uid=os.stat(path).st_uid if path.exists() else 0)
