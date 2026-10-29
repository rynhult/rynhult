"""Schedules must preserve a person's timing and durable launch intent."""
from datetime import datetime, timezone

import pytest
from helpers import pause_supervisor, write_db


def utc(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


@pytest.mark.parametrize('timing,zone,after,want', [
    ({'kind': 'once', 'at': '2026-10-23T09:00'}, 'America/Chicago', '2026-10-22T00:00', '2026-10-23T14:00'),
    ({'kind': 'once', 'at': '2026-10-23T09:00'}, 'America/Chicago', '2026-10-24T00:00', None),
    ({'kind': 'daily', 'time': '09:00'}, 'America/Chicago', '2026-10-22T15:00', '2026-10-23T14:00'),
    ({'kind': 'weekly', 'time': '09:00', 'weekday': 1}, 'UTC', '2026-10-22T15:00', '2026-10-26T09:00'),
    ({'kind': 'cron', 'expression': '*/15 9-10 * * 1-5'}, 'UTC', '2026-10-23T10:45', '2026-10-26T09:00'),
    ({'kind': 'cron', 'expression': '0 9 1 * 1'}, 'UTC', '2026-10-22T00:00', '2026-10-26T09:00'),
    ({'kind': 'daily', 'time': '02:30'}, 'America/Chicago', '2026-03-08T00:00', '2026-03-09T07:30'),
    ({'kind': 'daily', 'time': '01:30'}, 'America/Chicago', '2026-11-01T06:30', '2026-11-02T07:30'),
    ({'kind': 'daily', 'time': '09:00'}, 'Asia/Kathmandu', '2026-10-22T00:00', '2026-10-22T03:15'),
])
def test_next_run(timing, zone, after, want):
    from acc.server.schedules import next_time
    assert next_time(timing, zone, utc(after)) == (utc(want) if want else None)


@pytest.mark.parametrize('timing,zone', [
    ({'kind': 'cron', 'expression': '* * * *'}, 'UTC'),
    ({'kind': 'cron', 'expression': '60 * * * *'}, 'UTC'),
    ({'kind': 'cron', 'expression': '*/0 * * * *'}, 'UTC'),
    ({'kind': 'cron', 'expression': '0 0 31 2 *'}, 'UTC'),
    ({'kind': 'daily', 'time': '25:00'}, 'UTC'),
    ({'kind': 'weekly', 'time': '09:00', 'weekday': 9}, 'UTC'),
    ({'kind': 'once', 'at': '2026-03-08T02:30'}, 'America/Chicago'),
    ({'kind': 'daily', 'time': '09:00'}, 'Mars/Olympus'),
])
def test_invalid_timing_rejected(timing, zone):
    from acc.server.schedules import next_time
    with pytest.raises(ValueError):
        next_time(timing, zone, utc('2026-01-01T00:00'))

@pytest.fixture
async def person(api, project):
    from acc.server.api.context import Principal
    from acc.server.models import Project, User
    who = Principal('alex', 'member', api.app.state.ctx.settings.default_account)
    with write_db(api.app.state.db) as s:
        s.add(User(username='alex', role='member', account=who.account, password_hash='x', totp_secret='x'))
        s.get(Project, project['id']).owner = 'alex'
        s.commit()
    api.app.dependency_overrides[api.app.state.ctx.principal] = lambda: who
    return who


def payload(project, **extra):
    return {'launch': {'project_id': project['id'], 'provider': 'claude', 'name': 'Review',
                       'initial_prompt': 'Review the project', 'worktree': False},
            'timing': {'kind': 'daily', 'time': '09:00'}, 'timezone': 'America/Chicago',
            'enabled': True, **extra}


async def test_person_create_confirm_scope_and_audit(api, project, person):
    from sqlalchemy import select

    from acc.server.api.context import Principal
    from acc.server.models import AuditLog
    body = payload(project)
    body['launch']['permission_mode'] = 'bypassPermissions'
    assert (await api.post('/api/schedules', json=body)).status_code == 409
    body['launch']['confirm_dangerous'] = True
    created = await api.post('/api/schedules', json=body)
    assert created.status_code == 201, created.text
    row = created.json()
    assert row['owner'] == 'alex' and row['next_run_at'] and row['enabled']
    body['launch']['confirm_dangerous'] = False
    assert (await api.put(f"/api/schedules/{row['id']}", json=body)).status_code == 409
    api.app.dependency_overrides[api.app.state.ctx.principal] = lambda: Principal('blair', 'member', 'other')
    assert (await api.get('/api/schedules')).json() == []
    for method, tail in [('post', '/run'), ('post', '/disable'), ('delete', '')]:
        assert (await getattr(api, method)(f"/api/schedules/{row['id']}{tail}")).status_code == 404
    assert (await api.put(f"/api/schedules/{row['id']}", json=body)).status_code == 404
    api.app.dependency_overrides[api.app.state.ctx.principal] = lambda: Principal('owner', 'owner', person.account)
    body['launch']['confirm_dangerous'] = True
    assert (await api.put(f"/api/schedules/{row['id']}", json=body)).status_code == 200
    assert (await api.post(f"/api/schedules/{row['id']}/disable")).status_code == 200
    assert (await api.post(f"/api/schedules/{row['id']}/enable")).status_code == 200
    with api.app.state.db() as s:
        logs = list(s.scalars(select(AuditLog).where(AuditLog.action.like('schedule.%'))))
        assert {'schedule.create', 'schedule.update', 'schedule.enable', 'schedule.disable'} <= {x.action for x in logs}
        assert 'Review the project' not in str([x.detail for x in logs])


async def test_token_cannot_create_schedules(api, project):
    result = await api.post('/api/schedules', json=payload(project))
    assert result.status_code == 403


async def test_run_now_overlap_cancel_and_restart(api, project, person):
    from helpers import wait_for

    from acc.server.models import ScheduleRun, Session
    from acc.server.schedules import Scheduler
    created = (await api.post('/api/schedules', json=payload(project))).json()
    result = await api.post(f"/api/schedules/{created['id']}/run")
    assert result.status_code == 201, result.text
    run = result.json()
    async def launched():
        rows = (await api.get('/api/schedules')).json()
        return next((r for r in rows[0]['runs'] if r['id'] == run['id'] and r['session_id']), None)
    first = await wait_for(launched)
    with api.app.state.db() as s:
        session = s.get(Session, first['session_id'])
        assert session.owner == 'alex' and session.run_as == person.account
    second = (await api.post(f"/api/schedules/{created['id']}/run")).json()
    async def skipped():
        rows = (await api.get('/api/schedules')).json()
        return next((r for r in rows[0]['runs'] if r['id'] == second['id'] and r['status'] == 'skipped'), None)
    assert 'still active' in (await wait_for(skipped))['reason']
    # A new scheduler reconciles persisted intents; it must never create another session.
    await Scheduler(api.app.state.ctx).recover()
    with api.app.state.db() as s:
        assert s.get(ScheduleRun, run['id']).session_id == first['session_id']
    assert (await api.post(f"/api/schedules/{created['id']}/cancel")).status_code == 200
    with api.app.state.db() as s:
        assert s.get(Session, first['session_id']).desired_state == 'stopped'


async def test_budget_skip_and_failure_inbox(api, project, person, monkeypatch):
    from helpers import wait_for

    from acc.server import budgets
    made = (await api.post('/api/schedules', json=payload(project))).json()
    monkeypatch.setattr(budgets, 'paused', lambda *args: ['weekly budget'])
    await api.post(f"/api/schedules/{made['id']}/run")
    async def skipped():
        rows = (await api.get('/api/schedules')).json()
        return rows[0]['runs'] and rows[0]['runs'][0]['status'] == 'skipped'
    await wait_for(skipped)
    rows = (await api.get('/api/schedules')).json()
    assert 'weekly budget' in rows[0]['runs'][0]['reason']
    monkeypatch.setattr(budgets, 'paused', lambda *args: [])
    # A real refused launch: project is no longer trusted.
    from acc.server.models import Project
    with write_db(api.app.state.db) as s:
        s.get(Project, project['id']).trusted = False
        s.commit()
    await api.post(f"/api/schedules/{made['id']}/run")
    async def failed():
        return [i for i in (await api.get('/api/attention')).json() if i['kind'] == 'schedule_failed']
    assert 'Review' in (await wait_for(failed))[0]['title']


@pytest.mark.parametrize('policy,status', [('once', 'pending'), ('skip', 'skipped')])
async def test_recovery_coalesces_missed_runs(api, project, person, policy, status):
    from sqlalchemy import select

    from acc.server.models import Schedule, ScheduleRun
    from acc.server.schedules import Scheduler
    await pause_supervisor(api)
    made = (await api.post('/api/schedules', json=payload(project, missed=policy))).json()
    with write_db(api.app.state.db) as s:
        s.get(Schedule, made['id']).next_run_at = utc('2026-01-01T15:00')
        s.commit()
    scheduler = Scheduler(api.app.state.ctx)
    scheduler.materialize(utc('2026-10-22T16:00'))
    scheduler.materialize(utc('2026-10-22T16:00'))
    with api.app.state.db() as s:
        runs = list(s.scalars(select(ScheduleRun).where(ScheduleRun.schedule_id == made['id'])))
        assert len(runs) == 1 and runs[0].status == status
        assert s.get(Schedule, made['id']).next_run_at == utc('2026-10-23T14:00')


async def test_ambiguous_claim_fails_once_and_pending_survives_restart(api, project, person):
    from sqlalchemy import select

    from acc.server.models import Schedule, ScheduleRun, Session
    from acc.server.schedules import Scheduler, enqueue
    await pause_supervisor(api)
    made = (await api.post('/api/schedules', json=payload(project))).json()
    with write_db(api.app.state.db) as s:
        row = s.get(Schedule, made['id'])
        ambiguous = enqueue(s, row, utc('2026-10-22T00:00'))
        ambiguous.status = 'claimed'
        pending = enqueue(s, row, utc('2026-10-22T00:01'))
        s.commit()
        ambiguous_id, pending_id = ambiguous.id, pending.id
    scheduler = Scheduler(api.app.state.ctx)
    await scheduler.recover()
    await scheduler.dispatch(pending_id)
    await scheduler.dispatch(pending_id)
    await scheduler.recover()
    with api.app.state.db() as s:
        assert s.get(ScheduleRun, ambiguous_id).status == 'failed'
        assert s.get(ScheduleRun, pending_id).session_id
        assert len(list(s.scalars(select(Session)))) == 1


async def test_budget_enforced_before_worktree_and_repo_file_cannot_enable(api, project, person, env):
    from helpers import wait_for
    from sqlalchemy import select
    from test_budgets import seed

    from acc.server.models import Schedule, Session, User
    # Budget eligibility is currently owners only; exercise the real budget rule and observation.
    with write_db(api.app.state.db) as s:
        s.scalar(select(User).where(User.username == 'alex')).role = 'owner'
        s.commit()
    seed(api)
    from acc.server.api.context import Principal
    api.app.dependency_overrides[api.app.state.ctx.principal] = lambda: Principal('alex', 'owner', person.account)
    saved = await api.put('/api/budgets', json={'rules': {'claude.seven_day': {'mode': 'cap', 'limit': 80, 'action': 'pause'}}})
    assert saved.status_code == 200, saved.text
    body = payload(project)
    body['launch']['worktree'] = True  # not a git repo: would fail if broker ran before budget check
    made = (await api.post('/api/schedules', json=body)).json()
    await api.post(f"/api/schedules/{made['id']}/run")
    async def skipped():
        rows = (await api.get('/api/schedules')).json()
        return rows[0]['runs'] and rows[0]['runs'][0]['status'] == 'skipped'
    await wait_for(skipped)
    with api.app.state.db() as s:
        assert not list(s.scalars(select(Session)))
    assert not (env['workspaces'] / '.worktrees').exists()
    # Repo schedule discovery is intentionally unsupported; even a file asking for enable has no effect.
    folder = env['workspaces'] / 'demo' / '.acc'
    folder.mkdir()
    (folder / 'schedules.json').write_text('{"enabled": true, "prompt": "run me"}')
    with api.app.state.db() as s:
        assert len(list(s.scalars(select(Schedule)))) == 1


async def test_owner_account_change_skips_and_dangerous_codex_needs_confirmation(api, project, person):
    from acc.server.models import Project
    body = payload(project)
    body['launch'].update(provider='codex', sandbox='danger-full-access', approval='never')
    assert (await api.post('/api/schedules', json=body)).status_code == 409
    body['launch']['confirm_dangerous'] = True
    made = await api.post('/api/schedules', json=body)
    assert made.status_code == 201, made.text
    with write_db(api.app.state.db) as s:
        s.get(Project, project['id']).account = 'someone-else'
        s.commit()
    await api.post(f"/api/schedules/{made.json()['id']}/run")
    from helpers import wait_for
    async def skipped():
        rows = (await api.get('/api/schedules')).json()
        return rows[0]['runs'] and rows[0]['runs'][0]['status'] == 'skipped'
    await wait_for(skipped)


async def test_schedule_defaults_to_own_worktree_and_cancel_persists_stop_intent(api, project, person, monkeypatch):
    from helpers import wait_for

    from acc.server.models import Session
    body = payload(project)
    del body['launch']['worktree']
    made = (await api.post('/api/schedules', json=body)).json()
    assert made['launch']['worktree'] is True
    # Launch without a worktree for this non-git fixture, then simulate a crash just before the stop effect.
    body['launch']['worktree'] = False
    assert (await api.put(f"/api/schedules/{made['id']}", json=body)).status_code == 200
    await api.post(f"/api/schedules/{made['id']}/run")
    async def started():
        rows = (await api.get('/api/schedules')).json()
        return rows[0]['runs'] and rows[0]['runs'][0]['session_id']
    sid = await wait_for(started)
    async def interrupted(*args, **kwargs):
        raise RuntimeError('process interrupted before broker stop')
    monkeypatch.setattr(api.app.state.supervisor, 'stop', interrupted)
    with pytest.raises(Exception):
        await api.post(f"/api/schedules/{made['id']}/cancel")
    with api.app.state.db() as s:
        assert s.get(Session, sid).desired_state == 'stopped'


async def test_invalid_profile_rejected_before_schedule_saved(api, project, person):
    body = payload(project)
    body['launch']['profile'] = 'does-not-exist'
    response = await api.post('/api/schedules', json=body)
    assert response.status_code == 422


async def test_schedule_failure_push_is_personal(api, project, person):
    from acc.server.models import PushSubscription, Schedule
    from acc.server.schedules import Scheduler, enqueue
    await pause_supervisor(api)
    # The existing push transport is the external boundary; keep attention selection and routing real.
    delivered = []
    class Push:
        async def send(self, endpoint, p256dh, key, payload):
            delivered.append((endpoint, payload))
            return 201
    supervisor = api.app.state.supervisor
    supervisor.push = Push()
    with write_db(api.app.state.db) as s:
        s.add_all([PushSubscription(endpoint=f'https://push.example.com/{name}', p256dh='x', auth='x', username=name, label='Phone')
                   for name in ('alex', 'blair')])
        s.commit()
    await supervisor.push_attention()
    made = (await api.post('/api/schedules', json=payload(project))).json()
    with write_db(api.app.state.db) as s:
        run = enqueue(s, s.get(Schedule, made['id']), utc('2026-10-22T00:00'))
        Scheduler(api.app.state.ctx).finish(s, run, 'failed', 'Launch refused')
        s.commit()
    await supervisor.push_attention()
    assert [endpoint for endpoint, _ in delivered] == ['https://push.example.com/alex']


async def test_cancel_between_session_record_and_launch_never_starts_process(api, project, person, monkeypatch, tmux_has):
    import asyncio

    from helpers import reconcile_passes

    from acc.server.models import Session
    body = payload(project)
    made = (await api.post('/api/schedules', json=body)).json()
    supervisor = api.app.state.supervisor
    recorded, release, dispatched = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_dispatch = supervisor.scheduler.dispatch

    async def dispatch(run_id):
        try:
            await original_dispatch(run_id)
        finally:
            dispatched.set()

    monkeypatch.setattr(supervisor.scheduler, 'dispatch', dispatch)
    original_call = supervisor.broker.call
    async def gate(op, **kwargs):
        if op == 'trust_workspace':
            recorded.set()
            await release.wait()
        return await original_call(op, **kwargs)
    monkeypatch.setattr(supervisor.broker, 'call', gate)
    await api.post(f"/api/schedules/{made['id']}/run")
    await asyncio.wait_for(recorded.wait(), 15)
    rows = (await api.get('/api/schedules')).json()
    sid = rows[0]['runs'][0]['session_id']
    try:
        assert (await api.post(f"/api/schedules/{made['id']}/cancel")).status_code == 200
    finally:
        release.set()
    # Inspect durable state only after the real dispatch and subsequent reconciliation finish.
    await asyncio.wait_for(dispatched.wait(), 15)
    await reconcile_passes(api)
    with api.app.state.db() as s:
        session = s.get(Session, sid)
        assert session.desired_state == 'stopped'
        assert not tmux_has(session.tmux_name)
        assert not session.runs


async def test_profile_edits_cannot_widen_saved_launch_options(api, project, person):
    from helpers import wait_for
    body = payload(project)
    body['launch'].update(provider='cursor', profile='qa', skills=[], agents=[], mcps=[])
    made = await api.post('/api/schedules', json=body)
    assert made.status_code == 201, made.text
    api.app.state.supervisor.catalog.profiles['qa']['cursor'] = {'force': True}
    await api.post(f"/api/schedules/{made.json()['id']}/run")
    async def result():
        rows = (await api.get('/api/schedules')).json()
        return rows[0]['runs'] and rows[0]['runs'][0] if rows[0]['runs'] and rows[0]['runs'][0]['status'] not in ('pending', 'claimed') else None
    run = await wait_for(result)
    # A profile edit is not a person's new approval; the original safe launch still runs safely.
    assert run['session_id'], run
    session = (await api.get(f"/api/sessions/{run['session_id']}")).json()
    assert not session['launch_spec']['inputs'].get('options', {}).get('force', False)


async def test_account_change_while_launch_layout_is_loading_refuses_session(api, project, person, monkeypatch):
    import asyncio

    from helpers import wait_for
    from sqlalchemy import select

    from acc.server.models import Project, Session
    made = (await api.post('/api/schedules', json=payload(project))).json()
    supervisor = api.app.state.supervisor
    entered, release = asyncio.Event(), asyncio.Event()
    original = supervisor.layout
    async def layout(*args):
        entered.set()
        await release.wait()
        return await original(*args)
    monkeypatch.setattr(supervisor, 'layout', layout)
    await api.post(f"/api/schedules/{made['id']}/run")
    await asyncio.wait_for(entered.wait(), 5)
    with write_db(api.app.state.db) as s:
        s.get(Project, project['id']).account = 'other-agent'
        s.commit()
    release.set()
    async def finished():
        rows = (await api.get('/api/schedules')).json()
        return rows[0]['runs'][0] if rows[0]['runs'][0]['status'] not in ('pending', 'claimed') else None
    run = await wait_for(finished)
    assert run['status'] == 'failed', run
    with api.app.state.db() as s:
        assert not list(s.scalars(select(Session)))


async def test_schedule_keeps_explicit_team_permission_confirmation(api, project, person):
    from test_team import grant

    body = payload(project)
    body['launch'].update(worktree=True, confirm_dangerous=True,
                          orchestrator=grant(agents=[{'agent': 'claude', 'profile': None,
                                                     'permission_mode': 'bypassPermissions'}]))
    saved = await api.post('/api/schedules', json=body)
    assert saved.status_code == 201, saved.text
    assert saved.json()['launch']['confirm_dangerous'] is True


async def test_preview_lists_the_next_runs_as_the_scheduler_computes_them(api, project, person):
    """The schedule form's live "Next run" line (UX review R5) uses the scheduler's own timing."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    daily = (await api.post('/api/schedules/preview', json={'timing': {'kind': 'daily', 'time': '09:00'},
                                                             'timezone': 'America/Chicago'})).json()['next']
    assert len(daily) == 3
    instants = [datetime.fromisoformat(t) for t in daily]
    assert instants == sorted(set(instants))
    assert {(i.astimezone(ZoneInfo('America/Chicago')).hour, i.astimezone(ZoneInfo('America/Chicago')).minute) for i in instants} == {(9, 0)}
    once = (await api.post('/api/schedules/preview', json={'timing': {'kind': 'once', 'at': '2020-01-01T09:00'},
                                                            'timezone': 'UTC'})).json()
    assert once == {'next': []}  # in the past: nothing would run
    bad = await api.post('/api/schedules/preview', json={'timing': {'kind': 'cron', 'expression': '61 * * * *'}, 'timezone': 'UTC'})
    assert bad.status_code == 422 and 'cron' in bad.json()['detail']


async def test_token_cannot_preview_schedules(api):
    assert (await api.post('/api/schedules/preview', json={'timing': {'kind': 'daily', 'time': '09:00'}, 'timezone': 'UTC'})).status_code == 403


async def test_offline_workstation_occurrence_is_skipped_without_failure(api, project, person):
    from helpers import wait_for, write_db

    from acc.server.models import Node, Project
    with write_db(api.app.state.db) as db:
        db.add(Node(id='desk', name='demo-desk', status='active', role='workstation', info={'accounts': [person.account]}))
        db.get(Project, project['id']).host = 'demo-desk'
        db.commit()
    made = await api.post('/api/schedules', json=payload(project))
    assert made.status_code == 201, made.text
    run = (await api.post(f"/api/schedules/{made.json()['id']}/run")).json()
    async def skipped():
        rows = (await api.get('/api/schedules')).json()
        return next((row for row in rows[0]['runs'] if row['id'] == run['id'] and row['status'] == 'skipped'), None)
    result = await wait_for(skipped)
    assert 'Machine offline' in result['reason']
    assert result['session_id'] is None
    assert not any(item['kind'] == 'schedule_failed' for item in (await api.get('/api/attention')).json())
