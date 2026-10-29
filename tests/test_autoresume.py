import pytest
from test_remote_spool import sup  # noqa: F401

from acc.server import automation


@pytest.mark.parametrize('provider,text', [
    ('claude', "You've hit your limit · resets 2099-01-01T12:00:00Z"),
    ('codex', 'You have hit your usage limit. Try again at 2099-01-01T12:00:00Z'),
    ('copilot', 'You have exhausted your premium requests. Resets 2099-01-01T12:00:00Z'),
])
def test_limit_fixtures(provider, text):
    signal = automation.limit_signal(provider, text)
    assert signal and signal['reset'] == 4070952000
    assert automation.limit_signal(provider, 'ordinary task completed') is None


def test_reset_uses_latest_exhausted_window():
    assert automation.reset_time('codex', [{'provider': 'codex', 'used': 100, 'resets_at': 200},
                                          {'provider': 'codex', 'used': 100, 'resets_at': 300}], 400) == 300


def test_goal_validation():
    with pytest.raises(ValueError):
        automation.validate({'goal': {'text': 'work', 'max_continuations': 0, 'max_hours': 1}}, 'claude')
    with pytest.raises(ValueError):
        automation.validate({'autoresume': True}, 'cursor')


async def test_edit_and_human_cancel(api, project):
    r = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude', 'name': 'goal',
        'automation': {'autoresume': True, 'goal': {'text': 'Finish demo', 'max_continuations': 2, 'max_hours': 1}}})
    assert r.status_code == 201, r.text
    sid = r.json()['id']
    assert r.json()['automation']['goal']['used'] == 0
    r = await api.put(f'/api/sessions/{sid}/automation', json={'autoresume': False})
    assert r.status_code == 200 and not r.json()['automation']['autoresume']
    await api.app.state.supervisor.cancel_automation(sid)
    r = (await api.get(f'/api/sessions/{sid}')).json()
    assert r['automation']['goal']['status'] == 'person typed'


async def test_human_cancel_waits_for_a_concurrent_writer(sup):  # noqa: F811
    import asyncio
    import threading

    from helpers import write_db
    from sqlalchemy import event, text

    from acc.server.db import WORKER_WRITE, WRITE_INTENT
    from acc.server.models import LaunchSpec, Project, Session

    with write_db(sup.db) as db:
        db.add(Session(id="cancel-race", name="Cancel", tmux_name="acc-cancel", provider="claude",
                       project=Project(slug="cancel", name="Cancel", workspace_path="/w/cancel", account="agent"),
                       launch_spec=LaunchSpec(provider="claude", inputs={}, files={}, digest="x"),
                       run_as="agent",
                       automation={"goal": {"text": "work", "status": "running"}}))
        db.commit()
    engine = sup.db.kw["bind"]
    attempted = threading.Event()

    def beginning(connection, record, proxy):
        if WORKER_WRITE.get():
            attempted.set()

    token = WRITE_INTENT.set(True)
    try:
        writer = engine.connect()
        writer.begin()
    finally:
        WRITE_INTENT.reset(token)
    try:
        writer.execute(text("UPDATE session SET name='Edited' WHERE id='cancel-race'"))
        event.listen(engine, "checkout", beginning)
        task = asyncio.create_task(sup.cancel_automation("cancel-race"))
        try:
            assert await asyncio.wait_for(asyncio.to_thread(attempted.wait, 2), 3)
            writer.commit()
            await asyncio.wait_for(task, 3)
        finally:
            writer.rollback()
            await asyncio.gather(task, return_exceptions=True)
            event.remove(engine, "checkout", beginning)
    finally:
        writer.close()
    with sup.db() as db:
        session = db.get(Session, "cancel-race")
        assert session.name == "Edited"
        assert session.automation["goal"]["status"] == "person typed"


async def test_durable_reset_and_goal_bounds(api, project, monkeypatch):
    from acc.server.db import WRITE_INTENT
    from acc.server.models import Session
    monkeypatch.setattr(automation.budgets, 'windows', lambda db: [{'provider': 'claude', 'used': 100, 'resets_at': 200}])
    monkeypatch.setattr(automation.budgets, 'paused', lambda *a: [])
    r = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude', 'name': 'bounded'})
    sid = r.json()['id']
    token = WRITE_INTENT.set(True)
    try:
        with api.app.state.db() as db:
            s = db.get(Session, sid)
            automation.configure(db, s, {'autoresume': True}, '@api')
            s.observed_state = 'idle'
            assert automation.decide(db, s, {'reset': 300}, now=199) is None
            assert s.automation['waiting']['reset'] == 200
            db.commit()
        with api.app.state.db() as db:
            s = db.get(Session, sid)
            assert automation.decide(db, s, now=200)['text'].startswith('Your usage limit has reset.')
            db.commit()
        with api.app.state.db() as db:
            s = db.get(Session, sid)
            assert automation.decide(db, s, {'reset': 200}, now=201) is None
            automation.configure(db, s, {'goal': {'text': 'demo', 'max_continuations': 1, 'max_hours': 1}}, '@api')
            started = s.automation['goal']['started']
            assert automation.decide(db, s, turn='1', now=started)
            assert automation.decide(db, s, turn='2', now=started) is None
            assert s.automation['goal']['status'] == 'continuation cap reached'
    finally:
        WRITE_INTENT.reset(token)


@pytest.mark.parametrize('case', ['done', 'blocked: missing key', 'permission', 'time', 'budget', 'person'])
async def test_goal_stops(api, project, monkeypatch, case):
    from acc.server.db import WRITE_INTENT
    from acc.server.models import Session
    r = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude', 'name': 'stops'})
    token = WRITE_INTENT.set(True)
    try:
        with api.app.state.db() as db:
            s = db.get(Session, r.json()['id'])
            automation.configure(db, s, {'goal': {'text': 'demo', 'max_continuations': 2, 'max_hours': 1}}, '@api')
            s.observed_state = 'waiting_input' if case == 'permission' else 'idle'
            at = s.automation['goal']['started'] + (3601 if case == 'time' else 1)
            monkeypatch.setattr(automation.budgets, 'paused', lambda *a: ['weekly'] if case == 'budget' else [])
            if case == 'person':
                automation.cancel(db, s)
            assert automation.decide(db, s, assistant=f'GOAL: {case}', turn='1', now=at) is None
            if case == 'budget':
                assert 'budget' in s.automation['notice']
            else:
                assert s.automation['goal']['status'] != 'running'
    finally:
        WRITE_INTENT.reset(token)


async def test_cancel_does_not_rearm_same_reset(api, project, monkeypatch):
    from acc.server.db import WRITE_INTENT
    from acc.server.models import Session
    monkeypatch.setattr(automation.budgets, 'windows', lambda db: [])
    monkeypatch.setattr(automation.budgets, 'paused', lambda *a: [])
    r = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude', 'name': 'cancel'})
    token = WRITE_INTENT.set(True)
    try:
        with api.app.state.db() as db:
            s = db.get(Session, r.json()['id'])
            automation.configure(db, s, {'autoresume': True}, '@api')
            s.observed_state = 'idle'
            future = __import__('time').time() + 3600
            automation.decide(db, s, {'reset': future}, now=future - 2)
            automation.cancel(db, s)
            assert automation.decide(db, s, {'reset': future}, now=future + 1) is None
    finally:
        WRITE_INTENT.reset(token)


from test_ownership import two  # noqa: F401, F811
from test_users import csrf


async def test_edit_permissions(two):  # noqa: F811
    sid = two['session']['id']
    member = two['member']
    r = await member.put(f'/api/sessions/{sid}/automation', json={'autoresume': True}, headers=csrf(member))
    assert r.status_code == 404
    owner = two['owner']
    r = await owner.put(f'/api/sessions/{sid}/automation', json={'autoresume': True}, headers=csrf(owner))
    assert r.status_code == 200


@pytest.mark.parametrize('provider', ['claude', 'codex', 'copilot', 'cursor', 'antigravity'])
async def test_message_submitted_per_provider(api, project, env, provider):
    from helpers import wait_for
    r = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': provider, 'name': 'submit'})
    sid = r.json()['id']
    async def idle():
        return (await api.get(f'/api/sessions/{sid}')).json()['observed_state'] == 'idle'
    await wait_for(idle)
    r = await api.post(f'/api/sessions/{sid}/message', json={'text': 'actual submission'})
    assert r.status_code == 200
    async def received():
        p = env['run_dir'] / sid / 'fake.received'
        return p.exists() and 'actual submission' in p.read_text()
    await wait_for(received)


@pytest.mark.parametrize('provider', ['cursor', 'antigravity'])
def test_unsupported_provider_limit_control(provider):
    assert not automation.capability(provider)['autoresume']
    assert automation.capability(provider)['reason']
    assert automation.limit_signal(provider, "You've hit your limit") is None


def test_cli_printed_local_reset(monkeypatch):
    monkeypatch.setattr(automation.limit_signal.__globals__['time'], 'time', lambda: 1792674000)  # 2026-10-22 13:00 UTC
    result = automation.limit_signal('claude', "You've hit your limit · resets 4pm (America/Chicago)")
    assert result['reset'] == 1792702800  # 21:00 UTC
    assert automation.limit_signal('codex', "You've hit your usage limit. Try again in 2 hours")['reset'] == 1792681200


def test_submit_separates_input_bursts(tmp_path, monkeypatch):
    from test_session_tools import FakePane, broker_config

    from acc.broker import ops
    calls = []
    pane = FakePane()

    def tmux(*args, **kwargs):
        calls.append(args[1:])
        return pane.tmux(*args, **kwargs)

    monkeypatch.setattr(ops, 'tmux', tmux)
    monkeypatch.setattr(ops.time, 'sleep', lambda seconds: calls.append(('settle', seconds)))
    ops.send_text(broker_config(tmp_path), 'acc-demo', 'continue')
    assert calls[0][-1] == 'continue'
    assert calls[1] == ('settle', .2)
    assert calls[2][-1] == 'Enter'


async def test_usage_cap_and_missing_readings(api, project, monkeypatch):
    monkeypatch.setattr(automation.budgets, 'paused', lambda *a: [])
    from acc.server.db import WRITE_INTENT
    from acc.server.models import Session
    r = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude', 'name': 'usage'})
    token = WRITE_INTENT.set(True)
    try:
        with api.app.state.db() as db:
            s = db.get(Session, r.json()['id'])
            automation.configure(db, s, {'goal': {'text': 'demo', 'max_continuations': 2, 'max_hours': 1, 'max_usage': 80}}, '@api')
            s.observed_state = 'idle'
            monkeypatch.setattr(automation.budgets, 'windows', lambda db: [])
            assert automation.decide(db, s, turn='1') is None
            assert 'usage data' in s.automation['notice']
            monkeypatch.setattr(automation.budgets, 'windows', lambda db: [{'provider': 'claude', 'used': 81, 'stale': False}])
            assert automation.decide(db, s, turn='1') is None
            assert s.automation['goal']['status'] == 'usage cap reached'
    finally:
        WRITE_INTENT.reset(token)


async def test_cap_edits_preserve_progress_and_reset_counts(api, project, monkeypatch):
    from acc.server.db import WRITE_INTENT
    from acc.server.models import Session
    r = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude', 'name': 'caps'})
    token = WRITE_INTENT.set(True)
    try:
        with api.app.state.db() as db:
            s = db.get(Session, r.json()['id'])
            goal = {'text': 'demo', 'max_continuations': 1, 'max_hours': 1}
            automation.configure(db, s, {'autoresume': True, 'goal': goal}, '@api')
            started = s.automation['goal']['started']
            s.observed_state = 'idle'
            monkeypatch.setattr(automation.budgets, 'windows', lambda db: [])
            monkeypatch.setattr(automation.budgets, 'paused', lambda *a: [])
            assert automation.decide(db, s, {'reset': 200}, now=200)
            assert s.automation['goal']['used'] == 1
            automation.configure(db, s, {'goal': {**goal, 'max_hours': 2}}, '@api')
            assert s.automation['goal']['started'] == started
            assert s.automation['goal']['used'] == 1
            assert automation.decide(db, s, {'reset': 300}, now=300) is None
    finally:
        WRITE_INTENT.reset(token)


async def test_new_goal_ignores_old_completion_marker(api, project, monkeypatch):
    from acc.server import history
    from acc.server.db import WRITE_INTENT
    from acc.server.models import Session
    r = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude', 'name': 'new-goal'})
    token = WRITE_INTENT.set(True)
    try:
        with api.app.state.db() as db:
            s = db.get(Session, r.json()['id'])
            history.replace(db, s.id, [{'role': 'assistant', 'text': 'GOAL: done'}])
            automation.configure(db, s, {'goal': {'text': 'new work', 'max_continuations': 2, 'max_hours': 1}}, '@api')
            s.observed_state = 'idle'
            monkeypatch.setattr(automation.budgets, 'paused', lambda *a: [])
            assert automation.decide(db, s, assistant='GOAL: done', turn='0')
            assert s.automation['goal']['status'] == 'running'
    finally:
        WRITE_INTENT.reset(token)


async def test_crash_after_claim_is_not_replayed(api, project, monkeypatch):
    import time

    from acc.server.db import WRITE_INTENT
    from acc.server.models import Session
    r = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude', 'name': 'claim'})
    sid = r.json()['id']
    supervisor = api.app.state.supervisor
    sent = []
    async def broker_call(method, **kwargs):
        if method == 'automation_screen':
            return {'limit': {'reset': 1}, 'prompt': None}
        if method == 'send_text':
            sent.append(kwargs['text'])
            raise RuntimeError('simulate process death after durable claim')
        return {}
    monkeypatch.setattr(supervisor.broker, 'call', broker_call)
    token = WRITE_INTENT.set(True)
    try:
        async with supervisor._lock:
            with api.app.state.db() as db:
                s = db.get(Session, sid)
                s.observed_state = 'idle'
                s.automation = {'autoresume': True, 'waiting': {'reset': time.time() - 10, 'id': f'{sid}:reset:stable'}}
                db.commit()
                with pytest.raises(RuntimeError):
                    await supervisor._check_automation(db, s)
            with api.app.state.db() as db:
                s = db.get(Session, sid)
                assert s.automation['dispatch']['status'] == 'claimed'
                await supervisor._check_automation(db, s)
                assert len(sent) == 1
    finally:
        WRITE_INTENT.reset(token)


async def test_goal_marker_survives_search_truncation(api, project):
    from helpers import pause_supervisor

    # This is a direct history-write check, not a concurrent supervisor test.
    await pause_supervisor(api)
    from acc.server import history
    from acc.server.db import WRITE_INTENT
    from acc.server.models import Session
    r = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude', 'name': 'long-marker'})
    token = WRITE_INTENT.set(True)
    try:
        with api.app.state.db() as db:
            s = db.get(Session, r.json()['id'])
            automation.configure(db, s, {'goal': {'text': 'work', 'max_continuations': 2, 'max_hours': 1}}, '@api')
            history.replace(db, s.id, [{'role': 'assistant', 'text': 'x' * 9000 + '\nGOAL: done'}])
            assert s.automation['assistant_marker'] == {'seq': 0, 'text': 'GOAL: done'}
    finally:
        WRITE_INTENT.reset(token)


async def test_prompt_hooks_distinguish_automation_from_person(api, project):
    from acc.server.db import WRITE_INTENT
    from acc.server.models import Session
    r = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude', 'name': 'hooks'})
    token = WRITE_INTENT.set(True)
    try:
        with api.app.state.db() as db:
            s = db.get(Session, r.json()['id'])
            automation.configure(db, s, {'goal': {'text': 'demo', 'max_continuations': 2, 'max_hours': 1}}, '@api')
            s.automation = {**s.automation, 'expected_prompt': 'Continue demo'}
            api.app.state.supervisor._apply_hook(db, s, {'hook': {'hook_event_name': 'UserPromptSubmit', 'prompt': 'Continue demo'}})
            assert s.automation['goal']['status'] == 'running'
            api.app.state.supervisor._apply_hook(db, s, {'hook': {'hook_event_name': 'UserPromptSubmit', 'prompt': 'New instructions'}})
            assert s.automation['goal']['status'] == 'person typed'
    finally:
        WRITE_INTENT.reset(token)


async def test_launch_goal_is_the_first_prompt_when_none_given(api, project):
    r = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude', 'name': 'start-goal',
        'automation': {'goal': {'text': 'Build the demo', 'max_continuations': 2, 'max_hours': 1}}})
    assert r.status_code == 201
    assert 'Build the demo' in r.json()['launch_spec']['inputs']['initial_prompt']


async def test_interrupt_does_not_restart_goal(api, project):
    from helpers import wait_for
    r = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude', 'name': 'interrupt-goal',
        'automation': {'goal': {'text': 'demo', 'max_continuations': 2, 'max_hours': 1}}})
    sid = r.json()['id']
    async def idle():
        return (await api.get(f'/api/sessions/{sid}')).json()['observed_state'] == 'idle'
    await wait_for(idle)
    assert (await api.post(f'/api/sessions/{sid}/interrupt')).status_code == 200
    goal = (await api.get(f'/api/sessions/{sid}')).json()['automation']['goal']
    assert goal['status'] == 'person typed'


async def test_preview_includes_goal_instructions_and_prompt(api, project):
    r = await api.post('/api/sessions/preview', json={'project_id': project['id'], 'provider': 'claude', 'name': 'preview-goal',
        'automation': {'goal': {'text': 'Build the demo', 'max_continuations': 2, 'max_hours': 1}}})
    assert r.status_code == 200
    assert 'Build the demo' in r.json()['inputs']['initial_prompt']
    assert 'GOAL: done' in r.json()['files']['append-prompt.md']


def test_transient_rate_error_is_not_a_plan_limit():
    assert automation.limit_signal('claude', 'rate_limit_error: too many requests per minute') is None


def test_codex_cli_clock_reset_fallback(monkeypatch):
    import time
    from datetime import datetime, timedelta

    now = time.time()
    current = datetime.fromtimestamp(now).astimezone()
    target = (current + timedelta(hours=2)).replace(second=0, microsecond=0)
    printed = target.strftime('%I:%M %p')
    monkeypatch.setattr(automation.limit_signal.__globals__['time'], 'time', lambda: now)
    signal = automation.limit_signal('codex', f"You've hit your usage limit. Try again at {printed}.")
    assert signal['reset'] == target.timestamp()


@pytest.mark.parametrize('commit_during', ['list', 'workspace_changes', 'automation_screen'])
async def test_reconcile_refreshes_sessions_after_broker_list_commits(sup, caplog, commit_during):  # noqa: F811
    import sqlite3

    from acc.server.models import LaunchSpec, Placement, Project, Session, SessionRun
    from acc.server.supervisor_common import now

    with sup.db() as db:
        row = Session(id='list-race', name='Before', provider='claude', tmux_name='acc-list-race',
                      project=Project(slug='list-race', name='List race', workspace_path='/w/list-race', account='agent'),
                      launch_spec=LaunchSpec(provider='claude', inputs={}, files={}, digest='x'),
                      run_as='agent', desired_state='running', observed_state='idle', automation={'autoresume': False})
        row.runs.append(SessionRun(reason='launch', verified_at=now()))
        db.add(row)
        db.flush()
        row.placement = Placement(project_id=row.project_id, host='local', account='agent', path='/w/list-race')
        db.commit()
    if commit_during == 'automation_screen':
        import time
        sup._changes_checked = {'list-race': (time.monotonic(), now())}

    class Broker:
        default_account = 'agent'

        async def call(self, method, **kwargs):
            if method == commit_during:
                with sqlite3.connect(sup.db.kw['bind'].url.database) as other:
                    other.execute("UPDATE session SET name='Concurrent edit' WHERE id='list-race'")
                    other.commit()
            if method == 'list':
                return [{'tmux_name': 'acc-list-race', 'dead': False, 'last_output': 123, 'pid': 1}]
            if method in ('automation_screen', 'screen_prompt'):
                return {'prompt': 'permission'}
            if method == 'workspace_changes':
                return {'git': False}
            raise AssertionError(method)

    sup.broker = Broker()
    await sup.reconcile()
    assert 'reconcile failed' not in caplog.text
    with sup.db() as db:
        row = db.get(Session, 'list-race')
        assert row.name == 'Concurrent edit'
        assert row.last_output_at.timestamp() == 123
