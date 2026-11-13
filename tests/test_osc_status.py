"""Untrusted output can report a status, never a permission or escape payload."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from acc.broker.osc_status import MAX_PER_SECOND, MAX_SEQUENCE, StatusParser
from acc.server.reconcile import ReconcileMixin


@pytest.mark.parametrize('terminator', [b'\x07', b'\x1b\\'])
@pytest.mark.parametrize('state', ['working', 'done', 'blocked', 'error'])
def test_every_chunk_boundary(terminator, state):
    data = b'hello\x1b]7501;state=' + state.encode() + b': app=demo: future=ignored' + terminator
    for split in range(len(data) + 1):
        parser = StatusParser()
        results = [parser.feed(data[:split], 0), parser.feed(data[split:], 0)]
        assert [r for r in results if r] == [state]


@pytest.mark.parametrize('payload', [b'7501;state=approval', b'7501;app=working', b'7501;state=done:state=error',
                                     b'0;7501;state=done', b'7501;state=done\n', b'7501;state=done\xff',
                                     b'7501;state=done' + b'x' * 1000])
def test_bad_input(payload):
    parser = StatusParser()
    assert parser.feed(b'\x1b]' + payload + b'\x07', 0) is None
    assert parser.feed(b'\x1b]7501;state=done\x07', 1) == 'done'


def test_flood_and_unterminated_sequence_are_bounded():
    parser = StatusParser()
    assert parser.feed(b'\x1b]' + b'x' * 100_000, 0) is None
    assert len(parser.buffer) <= MAX_SEQUENCE
    parser.feed(b'\x07', 0)
    sequence = b'\x1b]7501;state=working\x07'
    assert parser.feed(sequence * (MAX_PER_SECOND + 1000), 0) == 'working'
    assert parser.feed(b'\x1b]7501;state=error\x07', 0) is None
    assert parser.feed(b'\x1b]7501;state=done\x07', 1) == 'done'


@pytest.mark.parametrize(('value', 'state'), [('working', 'active'), ('done', 'idle'),
                                              ('blocked', 'waiting_input'), ('error', 'idle')])
def test_reconciliation_status_only(value, state):
    owner = SimpleNamespace(_set_state=Mock())
    session = SimpleNamespace(id='test', observed_state='active', status_source='tmux', activity='old', last_error=None, spool_offset=0)
    db = Mock()
    db.scalar.return_value = None
    run = SimpleNamespace(id=7, started_at=datetime.now(timezone.utc))
    assert ReconcileMixin._check_osc_status(owner, db, session, run, {'osc7501': value})
    owner._set_state.assert_called_once_with(db, session, state, 'osc7501')
    assert not db.add.called  # no attention/approval records
    assert session.last_error == ('Script reported an error (OSC 7501)' if value == 'error' else None)


@pytest.mark.parametrize('source,hook_event', [('claude_hook', None), ('screen', 42)])
def test_hook_authority(source, hook_event):
    owner = SimpleNamespace(_set_state=Mock())
    session = SimpleNamespace(id='test', observed_state='active', status_source=source, spool_offset=0)
    db = Mock()
    db.scalar.return_value = hook_event
    run = SimpleNamespace(id=7, started_at=datetime.now(timezone.utc))
    assert not ReconcileMixin._check_osc_status(owner, db, session, run, {'osc7501': 'blocked'})
    owner._set_state.assert_not_called()


async def test_real_raw_output_observer(env):
    from helpers import control, wait_for
    from test_broker import base_params

    from acc.broker import ops

    params = base_params(env)
    ops.launch(env['broker'], params)
    run = env['run_dir'] / params['acc_id']
    await wait_for(lambda: (run / 'fake.argv.jsonl').exists() or ops.list_sessions(env['broker'])[0]['dead'])
    assert (run / 'fake.argv.jsonl').exists(), ops.terminal_output(env['broker'], params['tmux_name'])
    for value in ('working', 'blocked', 'error', 'done'):
        (run / 'fake.screen').write_bytes(b'\x1b]7501;state=' + value.encode() + b': app=ignored\x1b\\')
        control(env, params['acc_id'], 'screen')
        await wait_for(lambda: ops.list_sessions(env['broker'])[0].get('osc7501') == value)
    control(env, params['acc_id'], 'exit')
    await wait_for(lambda: ops.list_sessions(env['broker'])[0]['dead'])
    assert ops.list_sessions(env['broker'])[0]['osc7501'] is None
    assert ops.tmux(env['broker'], 'show-option', '-v', '-t', '=' + params['tmux_name'] + ':',
                    '@acc-osc7501').returncode != 0


async def test_first_output_is_observed_before_cli_start(env):
    from helpers import wait_for
    from test_broker import base_params

    from acc.broker import ops

    executable = env['run_dir'] / 'status-script'
    executable.write_text("#!/bin/sh\nprintf '\\033]7501;state=working\\007'\nexec sleep 60\n")
    executable.chmod(0o700)
    env['broker'].providers['cursor'] = str(executable)
    params = base_params(env, provider='cursor', args=[])
    try:
        ops.launch(env['broker'], params)
        await wait_for(lambda: ops.list_sessions(env['broker'])[0].get('osc7501') == 'working')
    finally:
        ops.kill(env['broker'], params['tmux_name'])


def test_observer_coalesces_updates_without_forwarding_payload(monkeypatch):
    from acc.broker import osc_status

    clock = [0.0]
    frames = iter([b'\x1b]7501;state=working\x07', b'\x1b]7501;state=blocked\x07',
                   b'\x1b]7501;state=error: app=not-retained\x07', b''])
    readiness = iter([True, True, True, False, True])
    writes = []

    def select_read(*args):
        ready = next(readiness)
        if not ready:
            clock[0] += args[3]
        return ([0] if ready else [], [], [])

    monkeypatch.setattr(osc_status.select, 'select', select_read)
    monkeypatch.setattr(osc_status.os, 'read', lambda *_: next(frames))
    monkeypatch.setattr(osc_status.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(osc_status.subprocess, 'run', lambda argv, **_: writes.append((clock[0], argv)))
    osc_status.observe('private-test-socket', 'acc-test')
    assert [(at, argv[-1]) for at, argv in writes] == [(0, 'working'), (1, 'error')]
    assert all(argv[-2] == '@acc-osc7501' and argv[-3] == '=acc-test:' for _, argv in writes)
    assert 'not-retained' not in str(writes)


def test_unrelated_osc_does_not_spend_budget():
    parser = StatusParser()
    assert parser.feed(b'\x1b]8;;https://example.com\x07' * 10_000 +
                       b'\x1b]7501;state=done\x07', 0) == 'done'
    assert parser.count == 1


@pytest.mark.parametrize('prefix', [b'\x1b]9999;', b'\x1b]7501;app='])
def test_nested_prefix_is_not_a_report(prefix):
    for split in range(len(prefix) + 1):
        parser = StatusParser()
        assert parser.feed(prefix[:split], 0) is None
        assert parser.feed(prefix[split:] + b'\x1b]7501;state=done\x07', 0) is None


@pytest.mark.parametrize('source', ['screen', 'claude_hook', 'codex_hook', 'api'])
@pytest.mark.parametrize('value', ['working', 'done', 'error', 'blocked'])
def test_osc_cannot_override_input_wait(source, value):
    owner = SimpleNamespace(_set_state=Mock())
    session = SimpleNamespace(id='test', observed_state='waiting_input', status_source=source)
    db = Mock()
    assert not ReconcileMixin._check_osc_status(owner, db, session, None, {'osc7501': value})
    owner._set_state.assert_not_called()
    db.scalar.assert_not_called()


def test_hook_query_cached_until_spool_changes():
    owner = SimpleNamespace(_set_state=Mock())
    session = SimpleNamespace(id='test', observed_state='active', status_source='tmux',
                              activity=None, last_error=None, spool_offset=0)
    run = SimpleNamespace(id=1, started_at=datetime.now(timezone.utc))
    db = Mock()
    db.scalar.return_value = None
    for _ in range(4):
        assert ReconcileMixin._check_osc_status(owner, db, session, run, {'osc7501': 'working'})
    assert db.scalar.call_count == 1
    session.spool_offset = 200
    db.scalar.return_value = 42
    for _ in range(4):
        assert not ReconcileMixin._check_osc_status(owner, db, session, run, {'osc7501': 'working'})
    assert db.scalar.call_count == 2
    session.spool_offset = 400
    assert not ReconcileMixin._check_osc_status(owner, db, session, run, {'osc7501': 'working'})
    assert db.scalar.call_count == 2


def test_wait_gate_timeout_is_visible(monkeypatch, capsys):
    import subprocess

    from acc.broker import osc_status

    def expired(*args, **kwargs):
        assert kwargs['timeout'] == 10
        raise subprocess.TimeoutExpired(args[0], 10)

    monkeypatch.setattr(osc_status.subprocess, 'run', expired)
    assert osc_status.wait_for_observer('private-test-socket', 'test-channel') == 1
    assert 'timed out waiting for the status observer' in capsys.readouterr().err


@pytest.mark.parametrize('pattern', [b'\x1b]7501;state=working\x07',
                                     b'\x1b]9999;' + b'x' * 200 + b'\x07', b'\x1b]0;x\x07',
                                     b'\x1b]9999;\x1b]7501;state=working\x07'],
                         ids=['status-flood', 'long-unrelated-osc', 'short-title-flood', 'nested-prefix-flood'])
async def test_real_pane_flood_keeps_tmux_memory_bounded(env, pattern):
    """Regression for tmux 3.4's unbounded unread pipe-pane output queue."""
    import asyncio
    import shlex
    import time
    from pathlib import Path

    from test_broker import base_params

    from acc.broker import ops

    config = env['broker']  # fixture-owned private tmux socket, never the account socket
    ops.prepare_tmux(config)
    ops.tmux(config, 'new-session', '-d', '-s', 'acc-baseline', 'sleep 60', check=True)
    pid = ops.tmux(config, 'display-message', '-p', '#{pid}', check=True).stdout.strip()

    def rss():
        line = next(line for line in Path(f'/proc/{pid}/status').read_text().splitlines()
                    if line.startswith('VmRSS:'))
        return int(line.split()[1]) * 1024

    baseline = rss()
    payload = env['run_dir'] / 'flood.bin'
    payload.write_bytes(pattern * ((1 << 20) // len(pattern)))
    script = env['run_dir'] / 'flood-script'
    script.write_text('#!/bin/sh\nwhile :; do cat ' + shlex.quote(str(payload)) + '; done\n')
    script.chmod(0o700)
    config.providers['cursor'] = str(script)
    params = base_params(env, provider='cursor', args=[])
    samples = []
    try:
        ops.launch(config, params)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            await asyncio.sleep(.25)
            samples.append(rss())
            assert samples[-1] < baseline + (48 << 20), f'tmux pipe backlog: {samples[-1] - baseline} bytes'
        assert ops.tmux(config, 'display-message', '-p', '-t', '=' + params['tmux_name'] + ':',
                        '#{pane_pipe}', check=True).stdout.strip() == '1'
        if pattern.startswith(b'\x1b]7501;'):
            assert ops.tmux(config, 'show-option', '-v', '-t', '=' + params['tmux_name'] + ':',
                            '@acc-osc7501', check=True).stdout.strip() == 'working'
        print(f'tmux RSS baseline={baseline}, peak={max(samples)}, growth={max(samples) - baseline} bytes')
    finally:
        ops.kill(config, params['tmux_name'])


async def test_screen_prompt_and_inbox_survive_osc_and_adoption(api, project, env):
    from helpers import control, pause_supervisor, wait_for, write_db
    from sqlalchemy import delete
    from test_supervisor import get, launch, state_is

    from acc.broker import ops
    from acc.server.models import Event, Session, SessionRun

    made = await launch(api, project, provider='cursor')
    sid = made['id']
    await wait_for(state_is(api, sid, 'idle'))
    await pause_supervisor(api)
    supervisor = api.app.state.supervisor
    with write_db(api.app.state.db) as db:
        # Model a hookless CLI after the fake provider's startup is settled.
        db.execute(delete(Event).where(Event.session_id == sid))
        db.get(Session, sid).status_source = 'tmux'
        db.commit()
    run_dir = env['run_dir'] / sid
    name = made['tmux_name']

    async def report(value):
        (run_dir / 'fake.screen').write_bytes(b'\x1b]7501;state=' + value.encode() + b'\x07')
        control(env, sid, 'screen')
        await wait_for(lambda: next(p for p in ops.list_sessions(env['broker'])
                                   if p['tmux_name'] == name)['osc7501'] == value)
        await supervisor.reconcile()

    await report('working')
    assert (await get(api, sid))['status_source'] == 'osc7501'
    control(env, sid, 'ask')
    await wait_for(lambda: ops.screen_prompt(env['broker'], name, 'cursor')['prompt'] is not None)
    await supervisor.reconcile()
    waiting = await get(api, sid)
    assert (waiting['observed_state'], waiting['status_source']) == ('waiting_input', 'screen')

    async def prompt_still_pending():
        row = await get(api, sid)
        assert (row['observed_state'], row['status_source']) == ('waiting_input', 'screen')
        inbox = (await api.get('/api/attention')).json()
        assert any(item['session_id'] == sid and item['kind'] == 'needs_input' for item in inbox)

    for value in ('done', 'error', 'blocked', 'working'):
        await report(value)
        await prompt_still_pending()
    with write_db(api.app.state.db) as db:
        db.execute(delete(SessionRun).where(SessionRun.session_id == sid))
        db.commit()
    await supervisor.reconcile()
    await prompt_still_pending()
    assert ops.tmux(env['broker'], 'show-option', '-v', '-t', '=' + name + ':', '@acc-osc7501').returncode != 0


def test_osc_transition_never_resolves_existing_attention():
    from acc.server.supervisor import Supervisor

    owner = SimpleNamespace(_attention_on_transition=Mock())
    session = SimpleNamespace(observed_state='waiting_input', idle_since=None,
                              project_id=None, id='test', management='external')
    db = Mock()
    Supervisor._set_state(owner, db, session, 'active', 'osc7501')
    owner._attention_on_transition.assert_not_called()
    assert db.add.call_args.args[0].verified is False


def test_observer_cannot_update_a_prefix_matching_session(env):
    import subprocess

    from acc.broker import ops
    from acc.python_child import module_command

    config = env['broker']
    ops.prepare_tmux(config)
    ops.tmux(config, 'new-session', '-d', '-s', 'acc-prefix-long', 'sleep 30', check=True)
    subprocess.run(module_command('acc.broker.osc_status', config.tmux_socket, 'acc-prefix'),
                   input=b'\x1b]7501;state=blocked\x07', timeout=5, check=True)
    assert ops.clear_osc_status(config, 'acc-prefix') == {'cleared': True}
    assert ops.tmux(config, 'show-option', '-v', '-t', '=acc-prefix-long:', '@acc-osc7501').returncode != 0


def test_real_launch_gate_wait_is_bounded(env, capsys):
    from acc.broker import ops
    from acc.broker.osc_status import wait_for_observer

    config = env['broker']
    ops.prepare_tmux(config)
    ops.tmux(config, 'new-session', '-d', '-s', 'acc-gate', 'sleep 30', check=True)
    assert wait_for_observer(config.tmux_socket, 'never-released', timeout=.1) == 1
    assert 'Restart this session' in capsys.readouterr().err


@pytest.mark.parametrize('provider,fixture,value,initial,expected', [
    ('copilot', 'copilot-working-screen.txt', 'done', 'idle', 'active'),
    ('codex', 'codex-error-idle.txt', 'working', 'active', 'idle'),
])
async def test_positive_screen_status_prevents_osc_flapping(api, project, env, monkeypatch,
                                                          provider, fixture, value, initial, expected):
    from pathlib import Path

    from helpers import control, pause_supervisor, wait_for, write_db
    from sqlalchemy import delete, select
    from test_supervisor import get, launch, state_is

    from acc.broker import ops
    from acc.server import orchestrator
    from acc.server.models import Event, Session

    made = await launch(api, project, provider=provider)
    sid, name = made['id'], made['tmux_name']
    await wait_for(state_is(api, sid, 'idle'))
    await pause_supervisor(api)
    supervisor = api.app.state.supervisor
    with write_db(api.app.state.db) as db:
        db.execute(delete(Event).where(Event.session_id == sid))
        row = db.get(Session, sid)
        row.observed_state, row.status_source = initial, 'tmux'
        db.commit()
    # Retain a contradictory report while real screen detection reports positive evidence.
    screen = (Path(__file__).parent / 'fixtures' / fixture).read_text()
    (env['run_dir'] / sid / 'fake.screen').write_text(
        '\x1b[2J\x1b[H' + screen + f'\x1b]7501;state={value}\x07')
    control(env, sid, 'screen')
    await wait_for(lambda: next(p for p in ops.list_sessions(env['broker'])
                               if p['tmux_name'] == name)['osc7501'] == value)
    await wait_for(lambda: (result := ops.screen_prompt(env['broker'], name, provider)) and
                   (result.get('working') if expected == 'active' else result.get('idle') is not None))
    transitions = Mock(wraps=orchestrator.state_changed)
    monkeypatch.setattr(orchestrator, 'state_changed', transitions)
    for _ in range(4):
        await supervisor.reconcile()
        current = await get(api, sid)
        assert (current['observed_state'], current['status_source']) == (expected, 'screen')
    # One screen transition; no OSC flip back, repeated state events, or repeated
    # orchestrator/worklist idle notifications from subsequent reconcile passes.
    assert [(call.args[2], call.args[3]) for call in transitions.call_args_list] == [(initial, expected)]
    with api.app.state.db() as db:
        events = list(db.scalars(select(Event).where(Event.session_id == sid, Event.kind == 'state')))
        assert [(event.source, event.payload) for event in events] == [
            ('screen', {'from': initial, 'to': expected})]
