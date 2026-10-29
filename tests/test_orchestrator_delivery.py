"""Slice 4: actual API/broker/fake provider paths."""
import json

import pytest
from helpers import wait_for
from sqlalchemy import select
from test_orchestrator_selection_api import assigned_start

from acc.server.orchestrator import delivery


@pytest.fixture(autouse=True)
def preview(monkeypatch):
    """The orchestrator v2 preview on (ACC_ORCHESTRATOR=1), before the app is built: without it, no v2 route exists."""
    monkeypatch.setenv('ACC_ORCHESTRATOR', '1')


@pytest.fixture
async def v2(api, monkeypatch):
    monkeypatch.setenv('RYNHULT_ORCHESTRATOR_V2_DELIVERY', '1')
    from acc.server import orchestrator
    monkeypatch.setattr(orchestrator, 'ON_STATE', [*orchestrator.ON_STATE, delivery.on_state])
    monkeypatch.setitem(orchestrator.TEAM_ACTIONS, 'report', delivery.team_report)
    monkeypatch.setitem(orchestrator.TEAM_FIELDS, 'report', {'status', 'summary', 'changed', 'tests', 'needs'})
    monkeypatch.setitem(orchestrator.TEAM_ACTIONS, 'message', delivery.team_message)
    monkeypatch.setitem(orchestrator.TEAM_ACTIONS, 'status', delivery.team_status)
    yield api
    await delivery.stop_worker(api.app.state.supervisor)


async def launched(api, project, provider="claude"):
    response = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': provider, 'name': 'Delivery'})
    assert response.status_code == 201, response.text
    sid = response.json()['id']
    await wait_for(lambda: idle(api, sid))
    return sid


async def idle(api, sid):
    data = (await api.get(f'/api/sessions/{sid}')).json()
    return data if data['observed_state'] == 'idle' else None


async def test_delivery_is_off_by_default(api, project):
    assert not delivery.enabled()
    response = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude', 'name': 'Off', 'task': 'Do this'})
    assert response.status_code == 422


async def test_project_topic_is_rejected_before_any_launch(api, project):
    response = await api.post('/api/sessions', json={'project_id': project['id'], 'topic': 'Wrong folder', 'provider': 'claude', 'name': 'Wrong'})
    assert response.status_code == 422
    assert 'did you mean task' in response.text
    assert (await api.get('/api/sessions')).json() == []


@pytest.mark.parametrize('provider', ['claude', 'codex', 'copilot', 'cursor', 'antigravity'])
async def test_message_lifecycle_real_broker_hook_ack(v2, project, provider):
    sid = await launched(v2, project, provider)
    response = await v2.post(f'/api/sessions/{sid}/message', json={'text': 'Review the changes carefully'})
    assert response.status_code == 202, response.text
    identifier = response.json()['delivery']['id']
    async def acknowledged():
        row = (await v2.get(f'/api/deliveries/{identifier}')).json()['delivery']
        return row if row['state'] == 'acknowledged' else None
    row = await wait_for(acknowledged, timeout=15)
    assert row['ack'] == 'hook:UserPromptSubmit'
    assert row['attempts'] == 1
    assert 'body' not in row


async def test_long_launch_task_uses_delivery_not_argv(v2, project):
    text = 'Review this task. ' + ('bounded detail ' * 1000)
    response = await v2.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude', 'name': 'Task', 'task': text})
    assert response.status_code == 201, response.text
    sid = response.json()['id']
    assert response.json()['delivery']['bytes'] == len(text.encode())
    sup = v2.app.state.supervisor
    from acc.server.models import Session
    with sup.db() as db:
        session = db.get(Session, sid)
        assert text not in json.dumps(session.launch_spec.inputs)
        assert text not in json.dumps(session.launch_spec.files)


async def test_prompt_safety_and_restart_never_retype(v2, project):
    sid = await launched(v2, project)
    sup = v2.app.state.supervisor
    await delivery.stop_worker(sup)
    identifier = delivery.enqueue(sup, sid, '@api', 'A queued instruction')['id']
    with sup.db() as db:
        row = db.get(delivery.MessageDelivery, identifier)
        row.state = 'typing'
        db.commit()
    delivery.recover(sup)
    with sup.db() as db:
        row = db.get(delivery.MessageDelivery, identifier)
        assert row.state == 'failed' and row.error == 'interrupted_uncertain'
        assert not row.body


@pytest.mark.parametrize('text', ['!whoami', '/approve', '#memory', 'bad\x1b[31m', 'x' * 65537])
async def test_dangerous_or_oversized_text_is_refused_before_launch(v2, project, text):
    response = await v2.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude', 'name': 'Bad', 'task': text})
    assert response.status_code == 422
    assert (await v2.get('/api/sessions')).json() == []


async def test_report_spool_is_bound_to_caller_and_needs_outbox_is_transactional(v2, project, env):
    from test_team import lead, request

    from acc.server.models import Event
    owner = await lead(v2, project, env)
    child = await assigned_start(v2, env, owner, name='Reporter', task='A report task')
    assert child['ok'], child
    result = await request(v2, env, child['result'], 'report', status='done', summary='Implemented the task',
        changed=[{'path': 'src/demo.py', 'kind': 'modified'}],
        tests=[{'command': 'python -m pytest tests/demo.py', 'passed': 3, 'failed': 0, 'skipped': 1}],
        needs=[{'title': 'Review the result', 'steps': 'Read the diff and choose whether to integrate it.'}])
    assert result['ok'], result
    report = result['result']
    assert report['session_id'] == child['result']['id']
    assert report['task_id'] and report['head'] and report['branch']
    got = await v2.get(f"/api/sessions/{child['result']['id']}/report")
    assert got.json()['report']['summary'] == 'Implemented the task'
    repeated = await request(v2, env, child['result'], 'report', status='done', summary='Overwrite')
    assert repeated['ok'] and repeated['result']['version'] == 2
    sup = v2.app.state.supervisor
    with sup.db() as db:
        row = next(r for r in delivery.pending_needs(db, owner['id']) if r.version == 1)
        delivery.mark_needs_ingested(row)
        db.rollback()
    with sup.db() as db:
        row = next(r for r in delivery.pending_needs(db, owner['id']) if r.version == 1)
        delivery.mark_needs_ingested(row)
        db.commit()
    with sup.db() as db:
        assert all(r.version == 2 and not r.needs for r in delivery.pending_needs(db, owner['id']))
        assert db.scalar(select(Event).where(Event.session_id == report['session_id'], Event.kind == 'team.member.report'))


async def test_report_cannot_forge_authority_or_execute_test_commands(v2, project, env, tmp_path):
    from test_team import lead, request
    owner = await lead(v2, project, env)
    forged = await request(v2, env, owner, 'report', status='done', summary='x', session_id='other')
    assert not forged['ok']
    marker = tmp_path / 'never-executed'
    command = f'touch {marker}'
    response = await request(v2, env, owner, 'report', status='partial', summary='Untrusted text',
        tests=[{'command': command, 'passed': 0, 'failed': 0, 'skipped': 0}])
    assert response['ok']
    assert response['result']['tests'][0]['command'] == command
    assert not marker.exists()


def test_echo_alone_or_folded_paste_is_never_acknowledgement():
    assert not delivery.screen_ack('old', 'new instruction', 'new instruction')
    assert not delivery.screen_ack('new instruction\n>', 'new instruction\n>', 'new instruction')
    assert not delivery.screen_ack('old', 'new instruction\n[Pasted Content]\n>', 'new instruction')
    assert delivery.screen_ack('old', 'new instruction\n>', 'new instruction')


async def test_queued_team_message_does_not_outlive_sender(v2, project, env, monkeypatch):
    from test_team import lead

    from acc.server.models import Session
    owner = await lead(v2, project, env)
    helper = (await assigned_start(v2, env, owner, name='Waiting', task='Work'))['result']
    await wait_for(lambda: idle(v2, helper['id']))
    sup = v2.app.state.supervisor
    await delivery.stop_worker(sup)
    identifier = delivery.enqueue(sup, helper['id'], f"session:{owner['id']}", 'Do not deliver after stop')['id']
    with sup.db() as db:
        db.get(Session, owner['id']).desired_state = 'stopped'
        db.commit()
    await delivery.process_once(sup)
    with sup.db() as db:
        row = db.get(delivery.MessageDelivery, identifier)
        assert row.state in ('failed', 'queued') and row.attempts == 0
    assert not (env['run_dir'] / helper['id'] / 'fake.received').exists()


@pytest.fixture
def raw_provider(env, tmp_path):
    """The fake CLI normally uses canonical input (Linux truncates those lines).
    A real TUI uses raw input; emulate it to exercise the full 64 KiB task path.
    """
    from pathlib import Path
    source = (Path(__file__).parent / 'fake_cli.py').read_text()
    source = source.replace('\nctl = run_dir / "fake.ctl"',
        '\nimport termios\nattrs = termios.tcgetattr(0)\nattrs[3] &= ~(termios.ICANON | termios.ECHO)\n'
        'termios.tcsetattr(0, termios.TCSANOW, attrs)\nctl = run_dir / "fake.ctl"')
    source = source.replace('line = sys.stdin.readline()', "line = sys.stdin.readline().lstrip('\\x1b\\x15')")
    compile(source, '<raw fake>', 'exec')
    binary = tmp_path / 'fake-claude-raw'
    binary.write_text(source)
    binary.chmod(0o700)
    env['broker'].providers['claude'] = str(binary)


@pytest.fixture
async def raw_v2(raw_provider, v2):
    return v2


async def test_full_64k_task_arrives_once_at_raw_fake_provider(raw_v2, project, env):
    text = 'Task ' + 'x' * (65536 - 5)
    response = await raw_v2.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude',
                                                     'name': 'Long raw task', 'task': text})
    assert response.status_code == 201, response.text
    row = response.json()['delivery']
    async def done():
        value = (await raw_v2.get(f"/api/deliveries/{row['id']}")).json()['delivery']
        return value if value['state'] in delivery.TERMINAL else None
    try:
        result = await wait_for(done, timeout=20)
    except AssertionError:
        state = (await raw_v2.get(f"/api/sessions/{response.json()['id']}")).json()
        status = (await raw_v2.get(f"/api/deliveries/{row['id']}")).json()
        raise AssertionError({'observed': state.get('observed_state'), 'error': state.get('last_error'),
                              'delivery_state': status['delivery']['state']}) from None
    assert result['state'] == 'acknowledged', {key: result[key] for key in ('state', 'error', 'ack', 'attempts')}
    received = env['run_dir'] / response.json()['id'] / 'fake.received'
    assert received.read_text() == text + '\n'


async def test_delivery_deadlines_follow_progress_not_the_clock(monkeypatch):
    """Review 2026-10-26: a 64 KiB task on a busy machine failed at a fixed 4 s after Enter while the provider was
    still echoing it. A delivery fails only after STALL_SECONDS with no progress, and never runs past LIMIT_SECONDS."""
    import asyncio
    monkeypatch.setattr(delivery, 'STALL_SECONDS', 0.2)
    monkeypatch.setattr(delivery, 'LIMIT_SECONDS', 0.8)

    async def steps(progress, every, seconds):
        for _ in range(round(seconds / every)):
            await asyncio.sleep(every)
            progress.moved()

    async with asyncio.timeout(delivery.STALL_SECONDS) as deadline:  # steady progress outlives the stall time
        await steps(delivery.Progress(deadline), 0.05, 0.5)
    with pytest.raises(TimeoutError):  # a stall ends it
        async with asyncio.timeout(delivery.STALL_SECONDS) as deadline:
            progress = delivery.Progress(deadline)
            await steps(progress, 0.05, 0.1)
            await asyncio.sleep(0.4)
    started = asyncio.get_running_loop().time()
    with pytest.raises(TimeoutError):  # progress that never ends stops at the limit
        async with asyncio.timeout(delivery.STALL_SECONDS) as deadline:
            await steps(delivery.Progress(deadline), 0.05, 5)
    assert 0.7 < asyncio.get_running_loop().time() - started < 1.5
    progress = delivery.Progress()  # the acknowledgement wait measures quiet time since the last output
    await asyncio.sleep(0.1)
    assert progress.quiet_for() >= 0.1
    progress.moved()
    assert progress.quiet_for() < 0.05


async def test_permission_prompt_is_never_answered_and_queue_expires(v2, project, env):
    from datetime import timedelta
    sid = await launched(v2, project)
    (env['run_dir'] / sid / 'fake.ctl').write_text('permission-request')
    async def waiting():
        return (await v2.get(f'/api/sessions/{sid}')).json()['observed_state'] == 'waiting_input'
    await wait_for(waiting)
    sup = v2.app.state.supervisor
    identifier = delivery.enqueue(sup, sid, '@api', 'Never answer the permission prompt')['id']
    await delivery.process_once(sup)
    with sup.db() as db:
        row = db.get(delivery.MessageDelivery, identifier)
        assert row.state == 'queued' and row.attempts == 0
        row.expires_at = delivery.now() - timedelta(seconds=1)
        db.commit()
    await delivery.process_once(sup)
    with sup.db() as db:
        assert db.get(delivery.MessageDelivery, identifier).state == 'expired'
    assert not (env['run_dir'] / sid / 'fake.received').exists()


async def test_redaction_happens_before_queue_storage(v2, project, monkeypatch):
    from acc.server.vault import Redactor
    sid = await launched(v2, project)
    sup = v2.app.state.supervisor
    await delivery.stop_worker(sup)
    monkeypatch.setattr(sup.vault, 'redactor', lambda *a: Redactor([('SECRET_CANARY_VALUE_123456789', 'demo')]))
    row = delivery.enqueue(sup, sid, '@api', 'Review SECRET_CANARY_VALUE_123456789 carefully')
    with sup.db() as db:
        stored = db.get(delivery.MessageDelivery, row['id'])
        assert 'SECRET_CANARY' not in stored.body
        assert '«key: demo»' in stored.body
    assert 'SECRET_CANARY' not in json.dumps(row, default=str)


async def test_committed_handoff_is_attached_as_partial_evidence(v2, project, env):
    import subprocess
    from pathlib import Path

    from test_team import lead
    owner = await lead(v2, project, env)
    helper = (await assigned_start(v2, env, owner, name='Handoff', task='Write a handoff'))['result']
    sup = v2.app.state.supervisor
    from acc.server.models import Session
    with sup.db() as db:
        cwd = Path(db.get(Session, helper['id']).cwd)
    name = f"docs/handoffs/2026-10-25-{helper['id']}-task.md"
    (cwd / name).parent.mkdir(parents=True)
    (cwd / name).write_text('A committed handoff; no structured completion claim.\n')
    subprocess.run(['git', '-C', str(cwd), 'add', name], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(cwd), '-c', 'user.name=Example', '-c', 'user.email=alex@example.com',
                    'commit', '-qm', 'Test handoff'], check=True, capture_output=True)
    assert (await v2.post(f"/api/sessions/{helper['id']}/stop")).status_code == 200
    await delivery.collect_missing_reports(sup)
    result = (await v2.get(f"/api/sessions/{helper['id']}/report")).json()['report']
    assert result['handoff_path'] == name
    assert result['status'] == 'partial'


@pytest.fixture
async def dropped_enter_v2(raw_provider, env, v2, request):
    binary = env['broker'].providers['claude']
    from pathlib import Path
    source = Path(binary).read_text()
    needle = "line = sys.stdin.readline().lstrip('\\x1b\\x15')"
    drops = getattr(request, 'param', 1)
    source = source.replace(needle, needle + f'''
        pending = run_dir / 'fake.pending'
        count = run_dir / 'fake.enters'
        if not line.strip() and pending.exists():
            line = pending.read_text()
        attempt = int(count.read_text()) + 1 if count.exists() else 1
        count.write_text(str(attempt))
        if attempt <= {drops}:
            pending.write_text(line)
            print(line.strip(), end='', flush=True)
            continue
''')
    compile(source, '<dropped Enter fake>', 'exec')
    Path(binary).write_text(source)
    return v2


@pytest.mark.parametrize('dropped_enter_v2', [1, 2], indirect=True)
async def test_only_one_enter_retry_and_never_a_second_paste(dropped_enter_v2, project, env, request):
    api = dropped_enter_v2
    sid = await launched(api, project)
    response = await api.post(f'/api/sessions/{sid}/message', json={'text': 'One literal paste'})
    identifier = response.json()['delivery']['id']
    async def completed():
        row = (await api.get(f'/api/deliveries/{identifier}')).json()['delivery']
        return row if row['state'] in delivery.TERMINAL else None
    result = await wait_for(completed, timeout=20)
    assert result['attempts'] == 2
    assert int((env['run_dir'] / sid / 'fake.enters').read_text()) == 2
    assert (env['run_dir'] / sid / 'fake.pending').read_text() == 'One literal paste\n'
    received = env['run_dir'] / sid / 'fake.received'
    if request.node.callspec.params['dropped_enter_v2'] == 1:
        assert result['state'] == 'acknowledged'
        assert received.read_text() == 'One literal paste\n'
    else:
        assert result['state'] == 'failed'
        assert not received.exists()


async def test_delivery_and_report_reads_do_not_cross_people(v2, project, env, member_accounts):
    from test_auth import do_setup
    from test_users import browser, csrf, join
    await do_setup(v2, env)
    sid = await launched(v2, project)
    sent = await v2.post(f'/api/sessions/{sid}/message', json={'text': 'Private owner context'})
    identifier = sent.json()['delivery']['id']
    link = (await v2.post('/api/users/invites', json={'role': 'member', 'account': 'dana-agent'})).json()['link']
    async with browser(v2) as member:
        assert (await join(member, link, 'alex')).status_code == 200
        assert (await member.get(f'/api/deliveries/{identifier}')).status_code == 404
        assert (await member.post(f'/api/deliveries/{identifier}/retry', headers=csrf(member))).status_code == 404
        assert (await member.get(f'/api/sessions/{sid}/deliveries')).status_code == 404
        assert (await member.get(f'/api/sessions/{sid}/report')).status_code == 404


async def test_stopping_then_resuming_does_not_revive_an_old_queued_instruction(v2, project):
    sid = await launched(v2, project)
    sup = v2.app.state.supervisor
    identifier = delivery.enqueue(sup, sid, '@api', 'Old instruction before stop')['id']
    assert (await v2.post(f'/api/sessions/{sid}/stop')).status_code == 200
    assert (await v2.post(f'/api/sessions/{sid}/resume')).status_code == 200
    with sup.db() as db:
        row = db.get(delivery.MessageDelivery, identifier)
        assert row.state == 'failed' and row.error == 'session_stopped'
        assert row.attempts == 0


async def test_person_taking_terminal_control_stops_remaining_delivery_keys(v2, project, env):
    import asyncio

    from acc.server.tty import Writers
    sid = await launched(v2, project)
    sup = v2.app.state.supervisor
    writers = Writers()
    sup._delivery_writers = writers
    identifier = delivery.enqueue(sup, sid, '@api', 'Do not type after terminal takeover')['id']
    sending = asyncio.create_task(delivery.process_once(sup))
    def typing():
        with sup.db() as db:
            return db.get(delivery.MessageDelivery, identifier).state == 'typing'
    await wait_for(typing, interval=.01)
    person = object()
    acquired, previous = writers.claim(sid, person, True)
    assert acquired and isinstance(previous, delivery.DeliveryWriter)
    await previous.close(code=4001, reason='person took control')
    await sending
    with sup.db() as db:
        row = db.get(delivery.MessageDelivery, identifier)
        assert row.state == 'failed' and row.attempts == 0
    assert writers.by_session[sid] is person
    assert not (env['run_dir'] / sid / 'fake.received').exists()


async def test_team_messages_carry_acc_s_origin_line(v2, project, env):
    """Review 2026-10-25 (threat review S4-5): the helper sees who sent it; a command prefix is still refused."""
    from test_team import lead, request
    owner = await lead(v2, project, env)
    helper = (await assigned_start(v2, env, owner, name='Writer', task='Work'))['result']
    await delivery.stop_worker(v2.app.state.supervisor)
    sent = await request(v2, env, owner, 'message', helper=helper['id'], text='Run the importer tests')
    assert sent['ok'], sent
    assert sent['result']['delivery']['preview'] == (
        '[Message from the team lead "Lead", an agent of this team, not from your person]: Run the importer tests')
    for text in ('!rm -rf build', '  /permissions', '# remember this'):
        refused = await request(v2, env, owner, 'message', helper=helper['id'], text=text)
        assert not refused['ok'] and 'command prefix' in refused['error']


def test_reports_lose_invisible_characters_and_refuse_them_in_paths():
    """Review 2026-10-25 (OV-9)."""
    fields = delivery.report_fields({'status': 'done', 'summary': 'Done\u202e.\nAll\u200b good', 'changed': [],
        'tests': [{'command': 'pytest\u2067 -q', 'passed': 1, 'failed': 0, 'skipped': 0}],
        'needs': [{'title': 'Approve\u202e it', 'steps': 'Line one\u200b\nLine two'}]})
    assert fields['summary'] == 'Done.\nAll good' and fields['tests'][0]['command'] == 'pytest -q'
    assert fields['needs'] == [{'title': 'Approve it', 'steps': 'Line one\nLine two'}]
    from acc.server.supervisor_common import LaunchRefused
    with pytest.raises(LaunchRefused):
        delivery.path('src/\u202eevil.py')


def test_clear_keys_are_reviewed_per_provider_and_never_include_esc():
    """Review 2026-10-25 (OV-11, threat review S4-3): Esc twice rewinds Claude Code and Codex; no provider gets Esc
    until one is verified to need it."""
    from acc import providers
    assert set(delivery.CLEAR_KEYS) == set(providers.PROVIDERS)
    for provider in providers.PROVIDERS:
        assert delivery.clear_keys(provider) == (b'\x15',), provider


@pytest.mark.parametrize('provider', ['claude', 'codex', 'copilot'])
async def test_open_terminal_delivery_reports_conflict_and_retry_preserves_single_message(v2, project, env, provider):
    from acc.server.tty import Writers
    sid = await launched(v2, project, provider)
    sup = v2.app.state.supervisor
    await delivery.stop_worker(sup)
    sup._delivery_writers = Writers()
    person = object()
    sup._delivery_writers.claim(sid, person, False)
    identifier = delivery.enqueue(sup, sid, '@api', 'One instruction only')['id']
    await delivery.process_once(sup)
    result = (await v2.get(f'/api/deliveries/{identifier}')).json()['delivery']
    assert result['state'] == 'queued' and result['error'] == 'terminal_in_use'
    assert result['notice'] == 'Not delivered: another window controls the terminal. Close it, then retry.'
    sup._delivery_writers.release(sid, person)
    response = await v2.post(f'/api/deliveries/{identifier}/retry')
    assert response.status_code == 200, response.text
    async def ack():
        return (await v2.get(f'/api/deliveries/{identifier}')).json()['delivery']['state'] == 'acknowledged'
    await wait_for(ack, timeout=15)
    assert (env['run_dir'] / sid / 'fake.received').read_text().splitlines() == ['One instruction only']


@pytest.fixture
def input_tui(env, tmp_path):
    """Raw-mode fake TUIs, with an editable prompt rather than canonical readline.

    Canonical fake_cli may already have read ghost text into Python's readline
    buffer: a kernel Ctrl-U cannot clear that, unlike a real TUI's editor.
    """
    from pathlib import Path
    source = (Path(__file__).parent / 'fake_cli.py').read_text()
    source = source.replace('\nctl = run_dir / "fake.ctl"', '''\nimport tty
    tty.setraw(0)
    pending_input = ""
    ctl = run_dir / "fake.ctl"'''.replace('    ', ''))
    start = source.index('        line = sys.stdin.readline()', source.index('# A real submitted terminal line:'))
    end = source.index('    if ctl.exists():', start)
    source = source[:start] + '''        character = os.read(0, 1).decode()
        if character == "\\x15":
            pending_input = ""
        elif character in ("\\r", "\\n"):
            if pending_input:
                with (run_dir / "fake.received").open("a") as received:
                    received.write(pending_input + "\\n")
                hook("UserPromptSubmit", prompt=pending_input)
                pending_input = ""
        else:
            pending_input += character
        sys.stdout.write("\\r\\x1b[2K> " + pending_input)
        sys.stdout.flush()
''' + source[end:]
    compile(source, '<input TUI>', 'exec')
    for provider in ('claude', 'codex', 'copilot'):
        binary = tmp_path / f'fake-{provider}-tui'
        binary.write_text(source)
        binary.chmod(0o700)
        env['broker'].providers[provider] = str(binary)


@pytest.fixture
async def input_v2(input_tui, v2):
    return v2


@pytest.mark.parametrize('provider', ['claude', 'codex', 'copilot'])
@pytest.mark.parametrize('case', ['idle', 'ghost', 'busy'])
async def test_provider_queue_clears_ghost_and_flushes_after_busy(input_v2, project, env, provider, case):
    v2 = input_v2
    import subprocess

    from acc.server.models import Session
    sid = await launched(v2, project, provider)
    sup = v2.app.state.supervisor
    await delivery.stop_worker(sup)
    if case == 'ghost':
        with sup.db() as db:
            name = db.get(Session, sid).tmux_name
        subprocess.run(['tmux', '-L', env['broker'].tmux_socket, 'send-keys', '-t', f'={name}:',
                        '-l', 'Unrelated suggested prompt'], check=True)
        async def ghost_visible():
            with sup.db() as db:
                account = db.get(Session, sid).run_as
            screen = await sup.broker.call('terminal_output', account=account, host='local', tmux_name=name)
            return '> Unrelated suggested prompt' in screen['text']
        await wait_for(ghost_visible)
    elif case == 'busy':
        with sup.db() as db:
            db.get(Session, sid).observed_state = 'active'
            db.commit()
    identifier = delivery.enqueue(sup, sid, '@api', 'Actual owner instruction')['id']
    await delivery.process_once(sup)
    if case == 'busy':
        with sup.db() as db:
            assert db.get(delivery.MessageDelivery, identifier).state == 'queued'
            assert not (env['run_dir'] / sid / 'fake.received').exists()
            db.get(Session, sid).observed_state = 'idle'
            db.commit()
        await delivery.process_once(sup)
    with sup.db() as db:
        row = db.get(delivery.MessageDelivery, identifier)
        assert row.state == 'acknowledged', (delivery.public(row), (env['run_dir'] / sid / 'fake.received').read_text() if (env['run_dir'] / sid / 'fake.received').exists() else 'no receipt')
    assert (env['run_dir'] / sid / 'fake.received').read_text().splitlines() == ['Actual owner instruction']


@pytest.mark.parametrize('provider', ['claude', 'codex', 'copilot'])
async def test_team_message_conflict_can_retry_same_delivery(v2, project, env, provider):
    from test_team import lead, request

    from acc.server.tty import Writers
    owner = await lead(v2, project, env, agents=[{'agent': provider, 'profile': None}])
    from unittest.mock import patch

    from test_orchestrator_selection_api import choose

    from acc.server.orchestrator import selection
    async def ready(supervisor, account, machines, agents):
        return {(machine, agent): {'logged_in': True, 'models': [{'id': 'model-a'}]}
                for machine in machines for agent in agents}
    with patch.object(selection, 'readiness', ready):
        picked = await choose(v2, env, owner, task='Explore the project', need={'task_type': 'discovery'})
        assert picked['ok'] and picked['result']['chosen'] is not None, picked
        picked = picked['result']
        started = await request(v2, env, owner, 'start', name='Writer',
                                assignment=picked['assignment_id'], selection=picked['id'])
        assert started['ok'], started
    helper = started['result']
    assert helper['provider'] == provider
    sup = v2.app.state.supervisor
    await delivery.stop_worker(sup)
    # A fresh helper's initial assignment must finish before this later message.
    from acc.server.models import Session
    with sup.db() as db:
        for row in db.scalars(select(delivery.MessageDelivery).where(delivery.MessageDelivery.session_id == helper['id'])):
            if row.state == 'queued':
                delivery.transition(db, row, 'failed', error='test_setup')
        db.get(Session, helper['id']).observed_state = 'idle'
        db.commit()
    sup._delivery_writers = Writers()
    person = object()
    sup._delivery_writers.claim(helper['id'], person, False)
    sent = await request(v2, env, owner, 'message', helper=helper['id'], text='Check the importer')
    identifier = sent['result']['delivery']['id']
    async def blocked():
        result = await request(v2, env, owner, 'status', helper=helper['id'])
        return any(row['id'] == identifier and row['error'] == 'terminal_in_use'
                   for row in result['result']['deliveries'])
    await wait_for(blocked)
    sup._delivery_writers.release(helper['id'], person)
    retried = await request(v2, env, owner, 'message', helper=helper['id'], delivery=identifier)
    assert retried['ok'], retried
    assert retried['result']['delivery']['id'] == identifier
    async def ack():
        with sup.db() as db:
            return db.get(delivery.MessageDelivery, identifier).state == 'acknowledged'
    await wait_for(ack, timeout=15)


async def test_retry_never_replays_a_submitted_or_uncertain_delivery(v2, project):
    sid = await launched(v2, project)
    sup = v2.app.state.supervisor
    await delivery.stop_worker(sup)
    identifier = delivery.enqueue(sup, sid, '@api', 'Do not replay')['id']
    with sup.db() as db:
        row = db.get(delivery.MessageDelivery, identifier)
        delivery.transition(db, row, 'failed', error='submission_unconfirmed')
        db.commit()
    response = await v2.post(f'/api/deliveries/{identifier}/retry')
    assert response.status_code == 409, response.text
    rows = (await v2.get(f'/api/sessions/{sid}/deliveries')).json()['deliveries']
    assert len(rows) == 1 and rows[0]['state'] == 'failed'
    assert 'Check the terminal' in rows[0]['notice']
    assert 'body' not in rows[0]


@pytest.mark.parametrize('provider', ['claude', 'codex', 'copilot'])
async def test_uncleared_input_is_reported_without_appending(input_v2, project, env, provider):
    v2 = input_v2
    import subprocess

    from acc.server.models import Session
    binary = env['broker'].providers[provider]
    from pathlib import Path
    path = Path(binary)
    path.write_text(path.read_text().replace('if character == "\\x15":\n            pending_input = ""',
                                            'if character == "\\x15":\n            pass'))
    sid = await launched(v2, project, provider)
    sup = v2.app.state.supervisor
    await delivery.stop_worker(sup)
    with sup.db() as db:
        name = db.get(Session, sid).tmux_name
        account = db.get(Session, sid).run_as
    subprocess.run(['tmux', '-L', env['broker'].tmux_socket, 'send-keys', '-t', f'={name}:', '-l', 'Ghost'], check=True)
    async def visible():
        screen = await sup.broker.call('terminal_output', account=account, host='local', tmux_name=name)
        return '> Ghost' in screen['text']
    await wait_for(visible)
    identifier = delivery.enqueue(sup, sid, '@api', 'Must not append')['id']
    await delivery.process_once(sup)
    with sup.db() as db:
        row = db.get(delivery.MessageDelivery, identifier)
        assert row.state == 'failed' and row.error == 'input_conflict', delivery.public(row)
    assert not (env['run_dir'] / sid / 'fake.received').exists()


@pytest.mark.parametrize('screen', ['> Ghost', '❯ Ghost', '› Ghost\n? for shortcuts', '│ ❯ Ghost │'])
def test_input_conflict_recognizes_provider_prompt_shapes(screen):
    assert delivery.input_conflict(screen)


@pytest.mark.parametrize('screen', ['> ', '❯ ', '› \n? for shortcuts', '│ ❯     │'])
def test_input_conflict_accepts_cleared_prompt_shapes(screen):
    assert not delivery.input_conflict(screen)


@pytest.mark.parametrize('status', ['done', 'partial', 'blocked'])
async def test_helper_report_queues_parent_notice_without_report_body(v2, project, env, status):
    from test_team import lead, request
    owner = await lead(v2, project, env)
    helper = (await assigned_start(v2, env, owner, name='Reporter', task='A task'))['result']
    sup = v2.app.state.supervisor
    await delivery.stop_worker(sup)
    result = await request(v2, env, helper, 'report', status=status, summary='UNTRUSTED REPORT BODY')
    assert result['ok'], result
    second = await request(v2, env, helper, 'report', status=status, summary='SECOND UNTRUSTED BODY')
    assert second['ok'], second
    with sup.db() as db:
        rows = list(db.scalars(select(delivery.MessageDelivery).where(
            delivery.MessageDelivery.session_id == owner['id'], delivery.MessageDelivery.actor == 'supervisor')))
        assert len(rows) == 1
        assert f'reported {status}' in rows[0].body
        assert result['result']['attempt_id'] in rows[0].body
        assert 'version 1' in rows[0].body
        assert 'version 2' in rows[0].body
        assert 'SECOND UNTRUSTED BODY' not in rows[0].body
        assert 'UNTRUSTED REPORT BODY' not in rows[0].body


async def test_helper_failures_coalesce_and_keep_inflight_notice(v2, project, env):
    from test_team import lead

    from acc.server.models import Session
    from acc.server.orchestrator import assignments
    owner = await lead(v2, project, env)
    helper = (await assigned_start(v2, env, owner, name='Reporter', task='A task'))['result']
    sup = v2.app.state.supervisor
    await delivery.stop_worker(sup)
    with sup.db() as db:
        session = db.get(Session, helper['id'])
        delivery.on_state(db, session, 'idle', 'failed')
        attempt = assignments.current(db, session.id)
        assignments.fail_attempt(db, attempt, 'supervisor', 'UNTRUSTED FAILURE')
        rows = list(db.scalars(select(delivery.MessageDelivery).where(
            delivery.MessageDelivery.session_id == owner['id'], delivery.MessageDelivery.actor == 'supervisor')))
        assert len(rows) == 1
        assert 'failed' in rows[0].body
        assert 'UNTRUSTED FAILURE' not in rows[0].body
        assert rows[0].deliver_after > rows[0].created_at
        rows[0].state = 'typing'
        delivery.on_state(db, session, 'idle', 'exited')
        rows = list(db.scalars(select(delivery.MessageDelivery).where(
            delivery.MessageDelivery.session_id == owner['id'], delivery.MessageDelivery.actor == 'supervisor')))
        assert len(rows) == 2


async def test_launch_failure_before_session_exists_notifies_assignment_creator(v2, project, env):
    import uuid

    from test_team import lead

    from acc.server.orchestrator import assignments
    owner = await lead(v2, project, env)
    helper = (await assigned_start(v2, env, owner, name='Reporter', task='A task'))['result']
    sup = v2.app.state.supervisor
    await delivery.stop_worker(sup)
    with sup.db() as db:
        old = assignments.current(db, helper['id'])
        attempt = assignments.Attempt(id=uuid.uuid4().hex, assignment_id=old.assignment_id, state='launching')
        db.add(attempt)
        db.flush()
        assignments.fail_attempt(db, attempt, 'supervisor', 'private launch error')
        row = db.scalar(select(delivery.MessageDelivery).where(
            delivery.MessageDelivery.session_id == owner['id'], delivery.MessageDelivery.actor == 'supervisor'))
        assert row is not None
        assert attempt.id in row.body and 'failed' in row.body
        assert 'private launch error' not in row.body


async def test_report_wakes_coordinating_parent_not_root_lead(v2, project, env):
    from test_team import lead, request

    from acc.server.models import Session
    owner = await lead(v2, project, env)
    parent = (await assigned_start(v2, env, owner, name='Coordinator', task='Coordinate'))['result']
    helper = (await assigned_start(v2, env, owner, name='Reporter', task='A task'))['result']
    sup = v2.app.state.supervisor
    await delivery.stop_worker(sup)
    with sup.db() as db:
        db.get(Session, helper['id']).parent_session_id = parent['id']
        db.commit()
    result = await request(v2, env, helper, 'report', status='blocked', summary='Need help')
    assert result['ok'], result
    with sup.db() as db:
        rows = list(db.scalars(select(delivery.MessageDelivery).where(delivery.MessageDelivery.actor == 'supervisor')))
        assert len(rows) == 1
        assert rows[0].session_id == parent['id']
        assert 'reported blocked' in rows[0].body
