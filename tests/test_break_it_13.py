"""Adversarial lane-C checks through isolated fake-provider brokers and repositories."""
from pathlib import Path

import pytest
from helpers import write_db
from sqlalchemy import select
from test_changes import git
from test_orchestrator_assignments import assigned
from test_orchestrator_assignments import runtime as _runtime
from test_team import request

from acc.server.models import Session
from acc.server.orchestrator import assignments, delivery

runtime = _runtime  # Reuse the established fake readiness/provider fixture.


async def start(api, env, caller, assignment, **options):
    selected = await request(api, env, caller, 'select', assignment=assignment['id'])
    assert selected['ok'], selected
    response = await request(api, env, caller, 'start', assignment=assignment['id'],
                             selection=selected['result']['id'], name='Boundary worker', **options)
    assert response['ok'], response
    return response['result']


def handoff(helper, text):
    root = Path(helper['worktree'])
    note = root / f"docs/handoffs/2026-10-27-{helper['id'][:8]}-evidence.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(text)
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'Record fixture evidence')
    return note


async def test_reading_superseded_report_keeps_its_own_committed_handoff(runtime, api, env, project):
    caller, assignment = await assigned(api, env, project)
    worker = await start(api, env, caller, assignment)
    handoff(worker, '# First report evidence\n')
    first = await request(api, env, worker, 'report', status='partial', summary='First report')
    assert first['ok'], first
    handoff(worker, '# Second report evidence\n')
    assert (await request(api, env, worker, 'report', status='done', summary='Second report'))['ok']
    result = await request(api, env, caller, 'read', helper=worker['id'], attempt=worker['id'], version=1)
    assert result['ok'], result
    assert result['result']['reports'][0]['summary'] == 'First report'
    assert [note['content'] for note in result['result']['handoffs']] == ['# First report evidence\n']


async def test_continuation_cannot_read_later_assignments_handoff_bytes(runtime, api, env, project):
    caller, first_task = await assigned(api, env, project)
    worker = await start(api, env, caller, first_task, kind='persistent')
    note = handoff(worker, '# Assignment A evidence\n')
    report = await request(api, env, worker, 'report', status='done', summary='A complete')
    assert report['ok'], report
    other = await request(api, env, caller, 'assign', goal='Separate assignment B', task_type='implementation',
                          need={'role': 'general', 'classification_reason': 'Independent work'})
    assert other['ok'], other
    with write_db(api.app.state.db) as db:
        db.get(Session, worker['id']).observed_state = 'idle'
        db.commit()
    given = await request(api, env, caller, 'give', helper=worker['id'], assignment=other['result']['id'])
    assert given['ok'], given
    late = await request(api, env, worker, 'report', attempt=worker['id'], status='done', summary='Late A report')
    assert not late['ok'], late
    # The persistent worker's branch advances during B. Its old handoff filename
    # can be edited too; that must not extend the evidence delegated to A's replacement.
    note.write_text('# PRIVATE B material outside replacement scope\n')
    (note.parents[2] / 'private-b.txt').write_text('B content must not appear on the replacement branch')
    git(Path(worker['worktree']), 'add', '.')
    git(Path(worker['worktree']), 'commit', '-qm', 'Independent B changes')
    replacement = await start(api, env, caller, first_task)
    assert not (Path(replacement['worktree']) / 'private-b.txt').exists()
    result = await request(api, env, replacement, 'read', helper=worker['id'], attempt=worker['id'])
    assert result['ok'], result
    assert result['result']['reports'][0]['summary'] == 'A complete'
    assert [v['content'] for v in result['result']['handoffs']] == ['# Assignment A evidence\n']
    denied = await request(api, env, replacement, 'read', helper=worker['id'], attempt=given['result']['attempt'])
    assert not denied['ok']
    with api.app.state.db() as db:
        assert db.get(assignments.Attempt, replacement['id']).brief['earlier_attempts'][0]['head'] == report['result']['head']
        assert len(list(db.scalars(select(delivery.TaskReport).where(delivery.TaskReport.attempt_id == worker['id'])))) == 1


async def test_concurrent_start_requests_create_one_attempt_and_one_checkout(runtime, api, env, project, monkeypatch):
    import asyncio
    caller, assignment = await assigned(api, env, project)
    selected = (await request(api, env, caller, 'select', assignment=assignment['id']))['result']
    supervisor = api.app.state.supervisor
    original, calls = supervisor.broker.call, []

    async def counted(operation, **kwargs):
        if operation == 'make_worktree':
            calls.append(kwargs)
        return await original(operation, **kwargs)
    monkeypatch.setattr(supervisor.broker, 'call', counted)
    replies = await asyncio.gather(*(request(api, env, caller, 'start', assignment=assignment['id'],
        selection=selected['id'], name='Concurrent worker') for _ in range(4)))
    assert all(reply['ok'] for reply in replies), replies
    assert len({reply['result']['id'] for reply in replies}) == len(calls) == 1
    with supervisor.db() as db:
        assert len(list(db.scalars(select(assignments.Attempt).where(assignments.Attempt.assignment_id == assignment['id'])))) == 1


async def test_launch_fence_survives_lost_memory_and_a_fresh_selection(runtime, api, env, project, monkeypatch):
    caller, assignment = await assigned(api, env, project)
    supervisor = api.app.state.supervisor
    original, calls = supervisor.broker.call, []

    async def lost_reply(operation, **kwargs):
        result = await original(operation, **kwargs)
        if operation == 'make_worktree':
            calls.append(result)
            raise RuntimeError('fixture: main lost broker reply after checkout creation')
        return result
    monkeypatch.setattr(supervisor.broker, 'call', lost_reply)
    selected = (await request(api, env, caller, 'select', assignment=assignment['id']))['result']
    first = await request(api, env, caller, 'start', assignment=assignment['id'], selection=selected['id'], name='Interrupted')
    assert not first['ok'] and len(calls) == 1
    # No session was ever committed: recovery may discard the clean checkout and
    # release this reservation. A later attempt is safe; the old test incorrectly
    # required this pre-launch failure to fence the assignment forever.
    assert not Path(calls[0]['path']).exists()
    with supervisor.db() as db:
        attempt = db.scalar(select(assignments.Attempt).where(assignments.Attempt.assignment_id == assignment['id']))
        assert attempt.state == 'failed' and attempt.session_id is None
    supervisor.__dict__.pop('_team_lock', None)
    monkeypatch.setattr(supervisor.broker, 'call', original)
    real_create, created = supervisor.create_session, []
    async def committed_but_lost(*args, **kwargs):
        session_id = await real_create(*args, **kwargs)
        created.append(session_id)
        raise RuntimeError('fixture: lost reply after session commit')
    monkeypatch.setattr(supervisor, 'create_session', committed_but_lost)
    selected = (await request(api, env, caller, 'select', assignment=assignment['id']))['result']
    again = await request(api, env, caller, 'start', assignment=assignment['id'], selection=selected['id'], name='Retry')
    assert not again['ok'] and len(created) == 1
    # Losing memory or obtaining a different selection must never duplicate a
    # committed session. Recovery adopts it rather than freeing its live slot.
    supervisor.__dict__.pop('_team_lock', None)
    fresh = (await request(api, env, caller, 'select', assignment=assignment['id']))['result']
    refused = await request(api, env, caller, 'start', assignment=assignment['id'], selection=fresh['id'], name='Duplicate')
    assert not refused['ok'] and len(created) == 1
    with supervisor.db() as db:
        attempts = list(db.scalars(select(assignments.Attempt).where(assignments.Attempt.assignment_id == assignment['id'])))
        assert len(attempts) == 2
        assert [(a.state, a.session_id) for a in attempts if a.session_id] == [('running', created[0])]
        assert db.get(Session, created[0]) is not None


@pytest.mark.parametrize('state', ['busy', 'stopped'])
async def test_give_refuses_busy_or_stopped_helpers_without_a_new_attempt(runtime, api, env, project, state):
    caller, assignment = await assigned(api, env, project)
    worker = await start(api, env, caller, assignment, kind='persistent')
    assert (await request(api, env, worker, 'report', status='done', summary='Finished'))['ok']
    other = (await request(api, env, caller, 'assign', goal='Next task', task_type='implementation',
                          need={'role': 'general', 'classification_reason': 'Next bounded task'}))['result']
    if state == 'stopped':
        assert (await request(api, env, caller, 'stop', helper=worker['id']))['ok']
    else:
        with write_db(api.app.state.db) as db:
            db.get(Session, worker['id']).observed_state = 'active'
            db.commit()
    result = await request(api, env, caller, 'give', helper=worker['id'], assignment=other['id'])
    assert not result['ok'], result
    with api.app.state.db() as db:
        assert not list(db.scalars(select(assignments.Attempt).where(assignments.Attempt.assignment_id == other['id'])))


async def test_foreign_attempt_reports_and_reads_cannot_cross_assignments_or_teams(runtime, api, env, project):
    from test_team import grant
    caller, assignment = await assigned(api, env, project)
    worker = await start(api, env, caller, assignment)
    second_task = (await request(api, env, caller, 'assign', goal='Independent task', task_type='implementation',
        need={'role': 'general', 'classification_reason': 'Separate evidence'}))['result']
    sibling = await start(api, env, caller, second_task)
    created = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude',
                             'name': 'Independent lead', 'worktree': True, 'orchestrator': grant()})
    assert created.status_code == 201, created.text
    other_lead = created.json()
    for actor in (sibling, other_lead):
        rejected = await request(api, env, actor, 'report', attempt=worker['id'], status='done', summary='Forged completion')
        assert not rejected['ok'], rejected
        rejected = await request(api, env, actor, 'read', helper=worker['id'], attempt=worker['id'])
        assert not rejected['ok'], rejected
    with api.app.state.db() as db:
        assert not list(db.scalars(select(delivery.TaskReport).where(delivery.TaskReport.attempt_id == worker['id'])))


@pytest.mark.parametrize('payload', [
    {'goal': 'x' * 2001}, {'acceptance': ['one'] * 21}, {'need': {'role': []}},
    {'need': {'role': 'general', 'requires': ['tool:missing'] * 41}},
    {'requirement_ids': ['../R-1']}, {'task_type': {'implementation': True}},
])
async def test_odd_or_oversized_assignments_leave_no_ledger_row(runtime, api, env, project, payload):
    from test_team import lead
    caller = await lead(api, project, env)
    result = await request(api, env, caller, 'assign', **{
        'goal': 'Bounded task', 'task_type': 'implementation',
        'need': {'role': 'general', 'classification_reason': 'Test refusal'}, **payload})
    assert not result['ok'], result
    with api.app.state.db() as db:
        assert not list(db.scalars(select(assignments.Assignment).where(assignments.Assignment.team_id == caller['id'])))


@pytest.mark.parametrize('attempt', ['', None, False, [], {}])
async def test_explicit_malformed_report_identity_never_defaults_to_current(runtime, api, env, project, attempt):
    caller, assignment = await assigned(api, env, project)
    worker = await start(api, env, caller, assignment)
    result = await request(api, env, worker, 'report', attempt=attempt, status='done', summary='Invalid explicit identity')
    assert not result['ok'], result
    with api.app.state.db() as db:
        assert not list(db.scalars(select(delivery.TaskReport).where(delivery.TaskReport.attempt_id == worker['id'])))


@pytest.mark.parametrize('missing_head', [True, False])
async def test_missing_pin_or_old_broker_never_falls_back_to_moving_handoff(runtime, api, env, project, monkeypatch, missing_head):
    caller, assignment = await assigned(api, env, project)
    worker = await start(api, env, caller, assignment)
    handoff(worker, '# Pinned evidence\n')
    saved = await request(api, env, worker, 'report', status='done', summary='Evidence')
    assert saved['ok'], saved
    supervisor = api.app.state.supervisor
    if missing_head:
        with write_db(supervisor.db) as db:
            row = db.scalar(select(delivery.TaskReport).where(delivery.TaskReport.attempt_id == worker['id']))
            row.head = None
            db.commit()
    original, calls = supervisor.broker.call, []

    async def old_broker(operation, **kwargs):
        if operation == 'team_handoffs':
            calls.append(kwargs['branch'])
            if not kwargs['branch'].startswith('acc/'):
                raise RuntimeError('fixture old broker rejects commit references')
        return await original(operation, **kwargs)
    monkeypatch.setattr(supervisor.broker, 'call', old_broker)
    read = await request(api, env, caller, 'read', helper=worker['id'], attempt=worker['id'])
    assert read['ok'], read
    assert read['result']['handoff_unavailable'] and read['result']['handoffs'] == []
    assert calls == ([] if missing_head else [saved['result']['head']])


async def test_explicit_empty_start_attempt_is_not_treated_as_new_launch(runtime, api, env, project):
    caller, assignment = await assigned(api, env, project)
    selected = (await request(api, env, caller, 'select', assignment=assignment['id']))['result']
    result = await request(api, env, caller, 'start', assignment=assignment['id'], selection=selected['id'],
                           attempt='', name='Malformed replay')
    assert not result['ok'], result
    with api.app.state.db() as db:
        assert not list(db.scalars(select(assignments.Attempt).where(assignments.Attempt.assignment_id == assignment['id'])))


async def test_pins_and_missing_role_or_tool_cannot_widen_the_grant(runtime, api, env, project):
    caller, assignment = await assigned(api, env, project)
    for pin in ({'agent': 'copilot'}, {'agent': 'claude', 'model': 'not-allowed'}):
        reply = await request(api, env, caller, 'select', assignment=assignment['id'], pin=pin)
        assert reply['ok'] and reply['result']['chosen'] is None, reply
    role = next(iter(api.app.state.supervisor.catalog.profiles))
    for need in ({'role': role}, {'role': 'general', 'requires': ['tool:not-in-profile']}):
        task = await request(api, env, caller, 'assign', goal='Restricted task', task_type='implementation',
                             need={'classification_reason': 'Hard filter probe', **need})
        assert task['ok'], task
        choice = await request(api, env, caller, 'select', assignment=task['result']['id'])
        assert choice['ok'] and choice['result']['chosen'] is None, choice
        if need['role'] != 'general':
            assert 'ask the person to add' in choice['result']['reason']
