"""Subscription automation admission through actual broker, queue and HTTP boundaries."""
import json
import subprocess
from types import SimpleNamespace

import pytest
from helpers import pause_supervisor, wait_for, write_db
from test_schedules import payload, person  # noqa: F401 — shared person fixture

from acc.broker import attach, ops
from acc.broker import claude_auth as broker_auth
from acc.providers.claude_auth import CODE, MESSAGE, classify
from acc.server import automation, claude_auth
from acc.server.models import Session
from acc.server.orchestrator import delivery

pytestmark = pytest.mark.claude_launch_auth


@pytest.fixture(autouse=True)
def launch_credentials(monkeypatch, request):
    """Existing API cases model Rynhult delivering vault credentials at launch."""
    from acc.server.vault import Vault
    original = Vault.deliver
    def deliver(*args, **kwargs):
        result = original(*args, **kwargs)
        mode = getattr(request.node, 'callspec', SimpleNamespace(params={})).params.get('mode', 'api_key')
        result.env.update({'CLAUDE_CODE_USE_BEDROCK': '1'} if mode == 'cloud' else
                          {'ANTHROPIC_API_KEY': 'fixture-key'})
        return result
    monkeypatch.setattr(Vault, 'deliver', deliver)


@pytest.mark.parametrize('data,expected', [
    ({'loggedIn': True, 'authMethod': method}, expected)
    for method, expected in [('claude.ai', 'subscription'), ('oauth_token', 'subscription'),
                             ('api_key', 'api_key'), ('api_key_helper', 'api_key'),
                             ('third_party', 'cloud'), ('none', 'unknown'), ('new_method', 'unknown')]
] + [(None, 'unknown'), ({}, 'unknown'), ({'loggedIn': False, 'authMethod': 'api_key'}, 'unknown')])
def test_auth_classification(data, expected):
    assert classify(data) == expected


@pytest.mark.parametrize('method', ['api_key', 'api_key_helper', 'third_party', 'claude.ai', 'oauth_token'])
def test_probe_uses_launch_settings_cwd_and_env(tmp_path, monkeypatch, method):
    def run(argv, **kwargs):
        assert argv == ['fake-claude', '--settings', '/fixture/settings.json', 'auth', 'status']
        assert kwargs['cwd'] == str(tmp_path)
        assert kwargs['env']['ANTHROPIC_API_KEY'] == 'fixture-key'
        assert kwargs['timeout'] == 5
        return subprocess.CompletedProcess(argv, 0, json.dumps({'loggedIn': True, 'authMethod': method}))
    monkeypatch.setattr(broker_auth.subprocess, 'run', run)
    assert broker_auth.probe('fake-claude', str(tmp_path), {'ANTHROPIC_API_KEY': 'fixture-key'},
                             ['--settings', '/fixture/settings.json', 'person launch text']) == classify(
                                 {'loggedIn': True, 'authMethod': method})


@pytest.mark.parametrize('failure', ['invalid', 'timeout', 'error', 'logged_out'])
def test_probe_fails_closed(tmp_path, monkeypatch, failure):
    def run(*args, **kwargs):
        if failure == 'timeout':
            raise subprocess.TimeoutExpired('fixture', 5)
        return subprocess.CompletedProcess([], 1 if failure == 'error' else 0,
                                           'invalid' if failure == 'invalid' else '{}')
    monkeypatch.setattr(broker_auth.subprocess, 'run', run)
    assert broker_auth.probe('fixture', str(tmp_path), {}, []) == 'unknown'


@pytest.mark.parametrize('mode', ['subscription', 'unknown'])
def test_broker_refuses_before_any_keys(env, monkeypatch, mode):
    monkeypatch.setattr(broker_auth, 'refresh', lambda *a, **kw: {'provider': 'claude', 'auth_mode': mode})
    monkeypatch.setattr(ops, 'tmux', lambda *a, **kw: pytest.fail('No keys permitted'))
    with pytest.raises(ops.OpError, match=MESSAGE):
        ops.send_text(env['broker'], 'acc-fixture-12345678', 'automated feedback', 'claude')
    # A caller claiming a different provider cannot bypass the actual launch context.
    with pytest.raises(ops.OpError, match=MESSAGE):
        ops.send_text(env['broker'], 'acc-fixture-12345678', 'automated feedback', 'codex')


@pytest.mark.parametrize('mode', ['subscription', 'unknown', 'api_key', 'cloud'])
async def test_automated_attach_and_person_terminal(env, monkeypatch, mode):
    monkeypatch.setattr(broker_auth, 'refresh', lambda *a, **kw: {'provider': 'claude', 'auth_mode': mode})
    class Writer:
        data = b''
        def write(self, data):
            self.data += data
        async def drain(self):
            pass
    monkeypatch.setattr(ops, 'tmux', lambda *a, **kw: SimpleNamespace(returncode=0, stdout=''))
    def terminal_open():
        raise RuntimeError('terminal allowed')
    monkeypatch.setattr(attach.os, 'openpty', terminal_open)
    writer = Writer()
    params = {'tmux_name': 'acc-fixture-12345678', 'readonly': False, 'automated': True}
    if mode in ('api_key', 'cloud'):
        with pytest.raises(RuntimeError, match='terminal allowed'):
            await attach.attach(env['broker'], params, None, writer)
    else:
        await attach.attach(env['broker'], params, None, writer)
        assert MESSAGE in writer.data.decode()
    params.pop('automated')
    with pytest.raises(RuntimeError, match='terminal allowed'):
        await attach.attach(env['broker'], params, None, Writer())


async def launch(api, project, **extra):
    result = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude',
                                                 'name': 'Auth guard', **extra})
    assert result.status_code == 201, result.text
    sid = result.json()['id']
    async def idle():
        row = (await api.get(f'/api/sessions/{sid}')).json()
        return row if row['observed_state'] == 'idle' else None
    await wait_for(idle)
    return sid


@pytest.mark.parametrize('mode', ['subscription', 'unknown', 'api_key', 'cloud'])
async def test_message_and_automation_api(api, project, monkeypatch, mode):
    monkeypatch.setattr(broker_auth, 'probe', lambda *a, **kw: mode)
    sid = await launch(api, project, initial_prompt='The person typed this launch prompt')
    allowed = mode in ('api_key', 'cloud')
    result = await api.post(f'/api/sessions/{sid}/message', json={'text': 'Review feedback'})
    assert result.status_code == (200 if allowed else 409), result.text
    if not allowed:
        assert result.json()['detail']['code'] == CODE
    result = await api.put(f'/api/sessions/{sid}/automation', json={'autoresume': True})
    assert result.status_code == (200 if allowed else 409), result.text
    result = await api.put(f'/api/sessions/{sid}/automation', json={'autoresume': False, 'goal': None})
    assert result.status_code == 200  # stopping remains possible


@pytest.mark.parametrize('mode', ['subscription', 'unknown', 'api_key', 'cloud'])
async def test_outbox_and_goal_configuration(api, project, monkeypatch, mode):
    monkeypatch.setattr(broker_auth, 'probe', lambda *a, **kw: mode)
    sid = await launch(api, project)
    await pause_supervisor(api)
    supervisor = api.app.state.supervisor
    with write_db(api.app.state.db) as db:
        session = db.get(Session, sid)
        claude_auth.store(db, session, mode)
        db.flush()
        def enqueue():
            return delivery.enqueue_in_transaction(db, supervisor, session, 'supervisor', 'team feedback')
        def configure():
            automation.configure(db, session, {'goal': {'text': 'Finish', 'max_continuations': 2,
                                                       'max_hours': 1}}, '@api')
        for operation in (enqueue, configure):
            if mode in ('api_key', 'cloud'):
                operation()
            else:
                with pytest.raises(claude_auth.AutomationRefused, match=MESSAGE):
                    operation()
        db.commit()


async def test_queued_delivery_rechecks_auth_before_stream(api, project, monkeypatch):
    monkeypatch.setattr(broker_auth, 'probe', lambda *a, **kw: 'api_key')
    sid = await launch(api, project)
    await pause_supervisor(api)
    supervisor = api.app.state.supervisor
    with write_db(api.app.state.db) as db:
        session = db.get(Session, sid)
        row = delivery.enqueue_in_transaction(db, supervisor, session, '@api', 'Continue')
        db.commit()
        identifier = row.id
    monkeypatch.setattr(broker_auth, 'probe', lambda *a, **kw: 'subscription')
    async def forbidden(*a, **kw):
        pytest.fail('No stream may open after auth changes')
    monkeypatch.setattr(supervisor.broker, 'open_stream', forbidden)
    await delivery.deliver(supervisor, identifier)
    with api.app.state.db() as db:
        row = db.get(delivery.MessageDelivery, identifier)
        assert row.state == 'failed' and row.error == CODE and row.body == ''


@pytest.mark.parametrize('mode', ['subscription', 'unknown', 'api_key', 'cloud'])
async def test_scheduled_launch_and_inbox(api, project, person, monkeypatch, mode):  # noqa: F811
    monkeypatch.setattr(broker_auth, 'probe', lambda *a, **kw: mode)
    made = await api.post('/api/schedules', json=payload(project))
    assert made.status_code == 201, made.text
    schedule_id = made.json()['id']
    result = await api.post(f'/api/schedules/{schedule_id}/run')
    assert result.status_code == 201, result.text
    async def settled():
        schedules = (await api.get('/api/schedules')).json()
        run = next(row for row in schedules if row['id'] == schedule_id)['runs'][0]
        expected = 'running' if mode in ('api_key', 'cloud') else 'skipped'
        if run['status'] != expected:
            return None
        if expected == 'running':
            session = (await api.get(f"/api/sessions/{run['session_id']}")).json()
            return run if session['observed_state'] == 'idle' else None
        return run
    run = await wait_for(settled)
    if mode in ('api_key', 'cloud'):
        assert run['status'] == 'running'
    else:
        assert run['status'] == 'skipped' and MESSAGE in run['reason']
        inbox = (await api.get('/api/attention')).json()
        assert any(MESSAGE in (item.get('detail') or '') for item in inbox)
        with api.app.state.db() as db:
            session = db.get(Session, run['session_id'])
            assert not session.runs  # no prompt or CLI launch happened
    assert (await api.post(f'/api/schedules/{schedule_id}/cancel')).status_code == 200


@pytest.mark.parametrize('kind', ['schedule', 'team', 'explanation'])
@pytest.mark.parametrize('mode', ['subscription', 'unknown', 'api_key', 'cloud'])
def test_automated_launch_before_cli(env, monkeypatch, kind, mode):
    from test_broker import base_params
    monkeypatch.setattr(broker_auth, 'probe', lambda *a, **kw: mode)
    def admitted(*args):
        raise RuntimeError('launch admitted')
    monkeypatch.setattr(ops, 'prepare_tmux', admitted)
    params = base_params(env)
    if kind != 'explanation':
        params['env'] = {'CLAUDE_CODE_USE_BEDROCK': '1'} if mode == 'cloud' else {'ANTHROPIC_API_KEY': 'fixture-key'}
    if kind == 'explanation':
        params['files'] = {'explanation.json': json.dumps({'model': 'test', 'native_id': '00000000-0000-0000-0000-000000000001'})}
        params['cwd'] = str(env['broker'].run_dir / params['acc_id'] / 'workspace')
    else:
        params['automated'] = True
    allowed = mode in ('api_key', 'cloud') and kind != 'explanation'
    expected = RuntimeError if allowed else ops.OpError
    with pytest.raises(expected, match='launch admitted' if allowed else MESSAGE):
        ops.launch(env['broker'], params)


@pytest.mark.parametrize('mode', ['subscription', 'unknown', 'api_key', 'cloud'])
@pytest.mark.parametrize('kind', ['goal', 'autoresume'])
async def test_continuation_dispatch_guard(api, project, monkeypatch, mode, kind):
    monkeypatch.setattr(broker_auth, 'probe', lambda *a, **kw: mode)
    sid = await launch(api, project)
    await pause_supervisor(api)
    supervisor = api.app.state.supervisor
    sent = []
    original = supervisor.broker.call
    async def call(method, **params):
        if method == 'send_text':
            sent.append(params['text'])
            return {}
        return await original(method, **params)
    monkeypatch.setattr(supervisor.broker, 'call', call)
    monkeypatch.setattr(automation, 'decide', lambda *a, **kw: {'text': f'{kind} continuation'})
    with api.app.state.db() as db:
        session = db.get(Session, sid)
        session.automation = {'autoresume': True, 'dispatch': {'status': 'claimed'}}
        db.commit()
        await supervisor._check_automation(db, session)
        assert bool(sent) == (mode in ('api_key', 'cloud'))
        if not sent:
            assert session.automation['notice'] == MESSAGE


@pytest.mark.parametrize('method,expected', [('claude.ai', 'subscription'), ('oauth_token', 'subscription'),
                                             ('api_key', 'api_key'), ('api_key_helper', 'api_key'),
                                             ('none', 'unknown')])
def test_documented_status_from_fake_cli(env, method, expected):
    run = env['run_dir'] / 'auth-fixture'
    run.mkdir(parents=True)
    (run / 'settings.json').write_text('{}')
    (run / 'fake-auth-method').write_text(method)
    assert broker_auth.probe(env['broker'].providers['claude'], str(env['workspaces']), {},
                             ['--settings', str(run / 'settings.json')]) == expected


@pytest.mark.parametrize('variable', ['CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY'])
def test_cloud_launch_environment(env, variable):
    assert broker_auth.probe(env['broker'].providers['claude'], str(env['workspaces']), {variable: '1'}, []) == 'cloud'


def test_broker_restart_loses_auth_authority(env):
    assert broker_auth.refresh(env['broker'], 'acc-existing')['auth_mode'] == 'unknown'


@pytest.mark.parametrize('mode', ['subscription', 'unknown', 'api_key', 'cloud'])
async def test_team_setup_launch_auth(api, project, env, monkeypatch, mode):
    from test_changes import repo
    from test_team import grant
    repo(env['workspaces'] / 'demo')
    monkeypatch.setattr(broker_auth, 'probe', lambda *a, **kw: mode)
    response = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude',
                                                   'name': 'Team auth', 'worktree': True, 'orchestrator': grant()})
    assert response.status_code == (201 if mode in ('api_key', 'cloud') else 409), response.text
    if response.status_code == 409:
        assert response.json()['detail']['code'] == CODE
        with api.app.state.db() as db:
            created = db.get(Session, response.json()['detail']['session_id'])
            assert created.desired_state == 'stopped' and not created.runs


@pytest.mark.parametrize('mode', ['subscription', 'unknown'])
def test_isolated_explanation_checks_its_own_auth(tmp_path, monkeypatch, mode):
    from acc.broker import explain
    run = tmp_path / 'explanation'
    run.mkdir()
    (run / 'explanation.json').write_text(json.dumps({'provider': 'claude', 'model': 'sonnet', 'native_id': 'fixture'}))
    monkeypatch.setattr(explain, 'isolated_home', lambda *a, **kw: None)
    def probe(*a, **kw):
        assert kw['inherit'] is False
        return mode
    monkeypatch.setattr(broker_auth, 'probe', probe)
    monkeypatch.setattr(explain.subprocess, 'Popen', lambda *a, **kw: pytest.fail('No explanation prompt permitted'))
    assert explain.run(run, 'fixture-claude', 'fixture-hook') == 1
    assert MESSAGE in json.loads((run / 'explanation-status.json').read_text())['error']


async def test_launch_automation_cannot_seed_its_own_counters(api, project):
    import time
    response = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude',
        'name': 'Goal counters', 'automation': {'goal': {'text': 'Finish the example', 'max_continuations': 2,
        'max_hours': 1, 'used': -999, 'started': time.time() + 100000}}})
    assert response.status_code == 201, response.text
    state = response.json()['automation']
    assert state['goal']['used'] == 0 and state['goal']['started'] <= time.time()
    assert 'pending_configuration' not in state


@pytest.mark.parametrize('spoof', ['home_helper', 'project_cloud', 'path_wrapper'])
@pytest.mark.parametrize('initial', ['subscription', 'unknown', 'api_key'])
def test_refresh_cannot_upgrade_from_writable_settings(env, monkeypatch, spoof, initial):
    config = env['broker']
    exe = config.providers['claude']
    context = dict(provider='claude', exe=exe, cwd=str(env['workspaces']),
                   env={'ANTHROPIC_API_KEY': 'fixture-key'}, args=[], container=False)
    config.auth_contexts['acc-fixture'] = context
    reported = [initial]
    def probe(path, *args, **kwargs):
        assert path == exe
        return reported[0]
    monkeypatch.setattr(broker_auth, 'probe', probe)
    assert broker_auth.refresh(config, 'acc-fixture')['auth_mode'] == initial
    folder = env['home'] / '.claude' if spoof == 'home_helper' else env['workspaces'] / '.claude'
    folder.mkdir(exist_ok=True)
    (folder / 'settings.json').write_text(json.dumps({'apiKeyHelper': 'echo fake',
                                                    'env': {'CLAUDE_CODE_USE_BEDROCK': '1'}}))
    if spoof == 'path_wrapper':
        wrapper = env['home'] / 'bin'
        wrapper.mkdir()
        (wrapper / 'claude').write_text('#!/bin/sh\nexit 0\n')
        (wrapper / 'claude').chmod(0o700)
        monkeypatch.setenv('PATH', str(wrapper))
    # Even a probe reporting allowed auth cannot upgrade a denied launch.
    reported[0] = 'cloud' if spoof == 'project_cloud' else 'api_key'
    actual = broker_auth.refresh(config, 'acc-fixture')['auth_mode']
    assert actual not in ('api_key', 'cloud') or initial == actual
    reported[0] = 'subscription'
    assert broker_auth.refresh(config, 'acc-fixture')['auth_mode'] == 'subscription'
    reported[0] = 'api_key'
    assert broker_auth.refresh(config, 'acc-fixture')['auth_mode'] not in ('api_key', 'cloud')


@pytest.mark.parametrize('reported', ['api_key', 'cloud'])
def test_settings_alone_cannot_authorize_launch(env, monkeypatch, reported):
    config = env['broker']
    config.auth_contexts['acc-fixture'] = dict(provider='claude', exe=config.providers['claude'],
        cwd=str(env['workspaces']), env={}, args=[], container=False)
    monkeypatch.setattr(broker_auth, 'probe', lambda *a, **kw: reported)
    assert broker_auth.refresh(config, 'acc-fixture')['auth_mode'] == 'unknown'


@pytest.mark.parametrize('queued', [False, True])
async def test_codex_message_after_broker_context_loss(api, project, env, monkeypatch, queued):
    monkeypatch.setenv('ACC_ORCHESTRATOR', '1')
    monkeypatch.setenv('RYNHULT_ORCHESTRATOR_V2_DELIVERY', '1' if queued else '0')
    result = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'codex', 'name': 'Restart'})
    assert result.status_code == 201, result.text
    sid = result.json()['id']
    async def idle():
        return (await api.get(f'/api/sessions/{sid}')).json()['observed_state'] == 'idle'
    await wait_for(idle)
    env['broker'].auth_contexts.clear()
    result = await api.post(f'/api/sessions/{sid}/message?wait=8', json={'text': 'Restart regression'})
    assert result.status_code == 200, result.text
    if queued:
        assert result.json()['delivery']['state'] == 'acknowledged', result.text
        await delivery.stop_worker(api.app.state.supervisor)


async def test_subscription_api_task_returns_typed_conflict(api, project, monkeypatch):
    monkeypatch.setenv('ACC_ORCHESTRATOR', '1')
    monkeypatch.setenv('RYNHULT_ORCHESTRATOR_V2_DELIVERY', '1')
    monkeypatch.setattr(broker_auth, 'probe', lambda *a, **kw: 'subscription')
    result = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude',
                                                'name': 'Task', 'task': 'Automation token task'})
    assert result.status_code == 409, result.text
    assert result.json()['detail']['code'] == CODE
    assert result.json()['detail']['session_id']
    assert f"project_id={project['id']}" in result.json()['detail']['keys']


@pytest.mark.parametrize('mode', ['subscription', 'unknown'])
async def test_person_api_task_is_launch_prompt(api, project, person, monkeypatch, mode):  # noqa: F811
    from dataclasses import replace
    monkeypatch.setenv('ACC_ORCHESTRATOR', '1')
    monkeypatch.setenv('RYNHULT_ORCHESTRATOR_V2_DELIVERY', '1')
    who = replace(person, user_id=1)
    api.app.dependency_overrides[api.app.state.ctx.principal] = lambda: who
    monkeypatch.setattr(broker_auth, 'probe', lambda *a, **kw: mode)
    response = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude',
        'name': 'Personal task', 'task': 'My own launch prompt'})
    assert response.status_code == 201, response.text
    with api.app.state.db() as db:
        session = db.get(Session, response.json()['id'])
        assert session.launch_spec.inputs['initial_prompt'] == 'My own launch prompt'
        from sqlalchemy import select

        from acc.server.models import Event
        event = db.scalar(select(Event).where(Event.session_id == session.id, Event.kind == 'run_launch'))
        assert 'My own launch prompt' in event.payload['command']
    assert 'delivery' not in response.json()


async def test_automated_attach_without_context(env, monkeypatch):
    class Writer:
        def write(self, data):
            pytest.fail(data.decode())
        async def drain(self):
            pass
    monkeypatch.setattr(ops, 'tmux', lambda *a, **kw: SimpleNamespace(returncode=0, stdout=''))
    def terminal_open():
        raise RuntimeError('terminal allowed')
    monkeypatch.setattr(attach.os, 'openpty', terminal_open)
    with pytest.raises(RuntimeError, match='terminal allowed'):
        await attach.attach(env['broker'], {'tmux_name': 'acc-fixture-12345678', 'automated': True}, None, Writer())


async def test_attach_refusal_preserves_subscription_notice(api, project, monkeypatch):
    sid = await launch(api, project)
    await pause_supervisor(api)
    supervisor = api.app.state.supervisor
    with write_db(api.app.state.db) as db:
        session = db.get(Session, sid)
        row = delivery.enqueue_in_transaction(db, supervisor, session, '@api', 'Continue')
        db.commit()
        identifier = row.id
    # Admission succeeds, but auth changes before the broker opens the terminal.
    async def refresh(*a):
        pass
    monkeypatch.setattr(claude_auth, 'refresh', refresh)
    original = broker_auth.refresh
    def broker_refresh(*a, **kw):
        return {'provider': 'claude', 'auth_mode': 'subscription'}
    monkeypatch.setattr(broker_auth, 'refresh', broker_refresh)
    await delivery.deliver(supervisor, identifier)
    with api.app.state.db() as db:
        row = db.get(delivery.MessageDelivery, identifier)
        assert row.state == 'failed' and row.error == CODE
    monkeypatch.setattr(broker_auth, 'refresh', original)


def test_launch_pins_absolute_cli_before_path_spoof(env, monkeypatch):
    from test_broker import base_params
    config = env['broker']
    executable = config.providers['claude']
    monkeypatch.setitem(config.providers, 'claude', 'fake-claude')
    monkeypatch.setenv('PATH', str(env['home'].parent / 'bin'))
    monkeypatch.setattr(broker_auth, 'probe', lambda *a, **kw: 'subscription')
    def admitted(*args):
        raise RuntimeError('launch admitted')
    monkeypatch.setattr(ops, 'prepare_tmux', admitted)
    params = base_params(env)
    # Preserve actual tmux lookup while the provider name is resolved via our fixture PATH.
    original = ops.tmux
    monkeypatch.setattr(ops, 'tmux', lambda *a, **kw: SimpleNamespace(returncode=1))
    with pytest.raises(RuntimeError, match='launch admitted'):
        ops.launch(config, params)
    assert config.auth_contexts[params['tmux_name']]['exe'] == executable
    monkeypatch.setattr(ops, 'tmux', original)


@pytest.mark.parametrize('spoof', ['home_helper', 'project_cloud', 'path_wrapper'])
def test_real_probe_spoof_cannot_unlock_subscription(env, monkeypatch, spoof):
    """Exercise an actual stand-in auth CLI reading agent-writable files."""
    from pathlib import Path
    home = env['home']
    cwd = env['workspaces']
    exe = home.parent / 'auth-cli'
    exe.write_text('''#!/usr/bin/python3
import json
from pathlib import Path
mode = 'claude.ai'
for folder in (Path.home(), Path.cwd()):
    settings = folder / '.claude/settings.json'
    if settings.exists():
        data = json.loads(settings.read_text())
        if data.get('apiKeyHelper'):
            mode = 'api_key_helper'
        if data.get('env', {}).get('CLAUDE_CODE_USE_BEDROCK') == '1':
            mode = 'third_party'
print(json.dumps({'loggedIn': True, 'authMethod': mode}))
''')
    exe.chmod(0o700)
    context = dict(provider='claude', exe=str(exe), cwd=str(cwd), env={}, args=[], container=False)
    config = env['broker']
    config.auth_contexts['acc-spoof'] = context
    assert broker_auth.refresh(config, 'acc-spoof')['auth_mode'] == 'subscription'
    expected = 'api_key'
    if spoof == 'path_wrapper':
        folder = home / '.local/bin'
        folder.mkdir(parents=True)
        wrapper = folder / 'claude'
        wrapper.write_text('#!/bin/sh\necho \'{"loggedIn":true,"authMethod":"api_key"}\'\n')
        wrapper.chmod(0o700)
        monkeypatch.setenv('PATH', str(folder))
        assert Path(context['exe']).is_absolute()
        expected = 'subscription'  # the pinned CLI ignores the wrapper
    else:
        folder = (home if spoof == 'home_helper' else cwd) / '.claude'
        folder.mkdir()
        settings = {'apiKeyHelper': 'echo fixture'} if spoof == 'home_helper' else {
            'env': {'CLAUDE_CODE_USE_BEDROCK': '1'}}
        (folder / 'settings.json').write_text(json.dumps(settings))
        expected = 'api_key' if spoof == 'home_helper' else 'cloud'
    assert broker_auth.probe(context['exe'], str(cwd), {}, []) == expected
    assert broker_auth.refresh(config, 'acc-spoof')['auth_mode'] not in ('api_key', 'cloud')


@pytest.mark.parametrize('provider', ['codex', 'copilot', 'opencode', 'claude'])
def test_listing_after_restart_keeps_provider(env, monkeypatch, provider):
    pane = {'tmux_name': 'acc-fixture', 'provider': provider, 'dead': False}
    monkeypatch.setattr(ops, '_read_sessions', lambda config: [dict(pane)])
    result = ops.list_sessions(env['broker'])[0]
    assert result['provider'] == provider
    assert result['auth_mode'] == 'unknown'


@pytest.mark.parametrize('mode', ['api_key', 'cloud'])
@pytest.mark.parametrize('provider', ['claude', 'codex', 'copilot', 'opencode'])
async def test_person_allowed_task_keeps_delivery(api, project, person, monkeypatch, provider, mode):  # noqa: F811
    from dataclasses import replace
    monkeypatch.setenv('ACC_ORCHESTRATOR', '1')
    monkeypatch.setenv('RYNHULT_ORCHESTRATOR_V2_DELIVERY', '1')
    api.app.dependency_overrides[api.app.state.ctx.principal] = lambda: replace(person, user_id=1)
    monkeypatch.setattr(broker_auth, 'probe', lambda *a, **kw: mode)
    sent = []
    async def send(sid, actor, text):
        sent.append(text)
        return {'id': 'fixture-delivery'}
    monkeypatch.setattr(api.app.state.supervisor, 'send_text', send)
    task = 'My own task' if provider == 'claude' else 'x' * 20001
    response = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': provider,
        'name': 'Delivered task', 'task': task})
    assert response.status_code == 201, response.text
    assert sent == [task]
    assert response.json()['delivery']['id'] == 'fixture-delivery'
    with api.app.state.db() as db:
        session = db.get(Session, response.json()['id'])
        assert not session.launch_spec.inputs.get('initial_prompt')


@pytest.mark.parametrize('length', [20000, 20001])
async def test_person_claude_task_prompt_limit(api, project, person, monkeypatch, length):  # noqa: F811
    from dataclasses import replace
    monkeypatch.setenv('ACC_ORCHESTRATOR', '1')
    monkeypatch.setenv('RYNHULT_ORCHESTRATOR_V2_DELIVERY', '1')
    api.app.dependency_overrides[api.app.state.ctx.principal] = lambda: replace(person, user_id=1)
    monkeypatch.setattr(broker_auth, 'probe', lambda *a, **kw: 'subscription')
    response = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude',
        'name': 'Bounded task', 'task': 'x' * length})
    assert response.status_code == (201 if length == 20000 else 422), response.text
