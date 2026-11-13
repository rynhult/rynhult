"""Resume eligibility is a read, including one-shot replacement grants."""
# ruff: noqa: F811
import base64
import hashlib
from datetime import timedelta

import pytest
from helpers import pause_supervisor
from sqlalchemy import event, select
from test_member_views import member  # noqa: F401 (fixture)

from acc.server.models import (
    AuditLog,
    Group,
    GroupLaunch,
    GroupOperation,
    LaunchSpec,
    Node,
    Session,
    Setting,
    Team,
    utcnow,
)
from acc.server.source_models import SessionTarget
from acc.server.supervisor_common import LaunchRefused


def snapshot(db):
    # Include every table, not just sessions: quota/audit/assignment writes count.
    return {table.name: [tuple(row) for row in db.execute(select(table))]
            for table in Session.metadata.sorted_tables}


def legacy_child(db, parent):
    """Bind a synthetic pre-Sources helper at INSERT, never reparent a saved root."""
    import uuid
    values = {column.name: getattr(parent, column.name) for column in Session.__table__.columns
              if column.name not in {'id', 'launch_spec_id', 'parent_session_id', 'parent_chain', 'root_owner', 'team_id'}}
    child = Session(**values, id=uuid.uuid4().hex[:12], parent_session_id=parent.id, team_id=parent.id)
    child.tmux_name = 'acc-preflight-' + child.id
    child.launch_spec = LaunchSpec(provider=parent.provider, inputs=dict(parent.launch_spec.inputs),
                                   files=dict(parent.launch_spec.files), digest=parent.launch_spec.digest)
    db.add(child)
    parent.team_id = parent.id
    return child


def bind_child_proof(db, session):
    if session.parent_session_id:
        import json

        from acc.server import child_caps, session_caps
        files = {**session.launch_spec.files, child_caps.FILE: json.dumps(child_caps.proof(db, db.get(Session, session.parent_session_id)))}
        files[session_caps.FILE] = session_caps.attach(db, session)
        session.launch_spec.files = files
        session.launch_spec.digest = child_caps.digest(files)


@pytest.mark.parametrize('case', [
    'eligible', 'deleted', 'explanation', 'external', 'private', 'generation',
    'archived', 'replacement', 'replacement-stop', 'team-stop', 'multirun-stop',
    'sleep-stop', 'team-stopped', 'team-expired', 'team-limit', 'team-limit-stopping',
    'assignment-failed', 'assignment-replaced', 'cancelled-prepare', 'cancelled',
    'prepare-write', 'prepare-unassigned', 'group-budget', 'group-access',
    'group-offline', 'group-quota', 'group-ceiling', 'group-eligible',
])
async def test_resume_preflight_equivalence_and_no_writes(api, project, monkeypatch, case):
    await pause_supervisor(api)
    made = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude', 'name': 'resume'})
    assert made.status_code == 201, made.text
    sid = made.json()['id']
    supervisor = api.app.state.supervisor
    with api.app.state.db() as db:
        session = db.get(Session, sid)
        needs_parent = case in ('team-stopped', 'team-expired', 'team-limit', 'team-limit-stopping') or case.startswith(('assignment-', 'cancelled', 'prepare-'))
        if needs_parent:
            session = legacy_child(db, session)
            sid = session.id
        parent_id = session.parent_session_id
        # These fixtures synthesize legacy remote/replacement rows after creation.
        # They predate Sources target freezing; location drift has its own route regression.
        if case.startswith('replacement') or case == 'group-offline':
            db.delete(db.get(SessionTarget, sid))
        session.desired_state = 'stopped'
        session.observed_state = 'stopped'
        if case == 'deleted':
            session.project = None
        elif case == 'explanation':
            session.launch_spec.files = {**session.launch_spec.files, 'explanation.json': '{}'}
        elif case == 'external':
            session.management, session.external_unit = 'external', 'outside.service'
        elif case == 'private':
            session.project.private_workspace = True
        elif case == 'generation':
            db.add(Setting(key='guardrails', value={'custom': ['new rule']}))
        if case in ('archived', 'replacement', 'replacement-stop'):
            session.desired_state = 'archived'
        if case.startswith('replacement'):
            key = base64.b64encode(b'public key').decode()
            db.add(Node(id='a' * 32, name='example', status='active', public_key=key))
            session.placement = None
            session.team_host = 'example'
            db.add(Setting(key='backup.replacement', value={
                'resume_sessions': [sid], 'reviewed_nodes': {'a' * 32: hashlib.sha256(b'public key').hexdigest()},
                'resume_bindings': {sid: {'node_id': 'a' * 32, 'account': session.run_as, 'native_id': session.native_id}}}))
        if case in ('team-stop', 'replacement-stop'):
            session.team_id, session.team_stop_pending = sid, True
        if case == 'multirun-stop':
            session.multirun_stop_pending = True
        if case == 'sleep-stop':
            session.sleep_paused_at, session.last_error = utcnow(), 'stop not confirmed: offline'
        if case in ('team-stopped', 'team-expired', 'team-limit', 'team-limit-stopping'):
            session.team_id = parent_id
            db.add(Team(id=parent_id, stopped=case == 'team-stopped', limits={'at_once': 1 if case.startswith('team-limit') else 2},
                        expires_at=utcnow() - timedelta(seconds=1) if case == 'team-expired' else None))
        if case.startswith('team-limit'):
            db.add(Session(id='c' * 32, project_id=session.project_id, provider=session.provider, name='Other helper',
                           tmux_name='other-helper', launch_spec=session.launch_spec, run_as=session.run_as,
                           team_id=session.team_id, parent_session_id=session.parent_session_id,
                           desired_state='running' if case == 'team-limit' else 'stopped',
                           team_stop_pending=case == 'team-limit-stopping', observed_state='stopped'))
        if case.startswith(('assignment-', 'cancelled', 'prepare-')):
            from acc.server.orchestrator.assignments import Assignment, Attempt
            from acc.server.orchestrator.planning import OrchestrationPlan
            session.team_id = parent_id
            db.add(Team(id=parent_id, limits={'at_once': 2}))
            db.flush()
            if case not in ('prepare-write', 'prepare-unassigned'):
                db.add(Assignment(id='c' * 32, team_id=session.team_id, created_by=sid,
                                  task_type='implementation', goal='test', need={}, state='blocked'))
                db.flush()
                db.add(Attempt(id='d' * 32, assignment_id='c' * 32, session_id=sid, sequence=0,
                               state='failed' if case == 'assignment-failed' else 'cancelled'))
                if case == 'assignment-replaced':
                    db.add(Attempt(id='e' * 32, assignment_id='c' * 32, session_id=None, sequence=1))
            if case in ('prepare-write', 'prepare-unassigned', 'cancelled-prepare'):
                db.add(OrchestrationPlan(team_id=session.team_id, run_mode='prepare'))
                if case == 'prepare-unassigned':
                    session.launch_spec.inputs = {**session.launch_spec.inputs, 'permission_mode': 'plan'}
        if case.startswith('group-'):
            from acc.server import budgets, group_board, group_remote
            group = Group(id='f' * 32, name='Test group', created_by='@api')
            db.add(group)
            db.flush()
            session.project.group_id = group.id
            session.project.owner = None
            # Isolate authorization/readiness and budget observations; real quota
            # queries, replacement grants, saved generation and writes remain real.
            monkeypatch.setattr(budgets, 'paused', lambda *a, **kw: ['budget'] if case == 'group-budget' else [])
            monkeypatch.setattr(group_board, 'authorized', lambda *a: case != 'group-access')
            if case == 'group-offline':
                session.placement = None
                session.team_host = 'offline'
                monkeypatch.setattr(group_remote, 'source_placement', lambda *a: None)
            if case == 'group-quota':
                group.concurrent = 0
            if case == 'group-ceiling':
                group.override_until = utcnow() + timedelta(hours=1)
                group.concurrent_ceiling = 0
        bind_child_proof(db, session)
        db.commit()
    with api.app.state.db() as db:
        before = snapshot(db)
        session = db.get(Session, sid)
        def read_only(connection, cursor, statement, parameters, context, many):
            assert statement.lstrip().upper().startswith('SELECT'), statement
        connection = db.connection()
        event.listen(connection, 'before_cursor_execute', read_only)
        try:
            reason = supervisor.resume_preflight(db, session, '@api')
            assert not db.new and not db.dirty and not db.deleted
            assert snapshot(db) == before
        finally:
            event.remove(connection, 'before_cursor_execute', read_only)
        db.commit()  # even a careless reader committing cannot consume the grant
    if case in ('eligible', 'replacement', 'cancelled', 'group-eligible'):
        assert reason is None
        await supervisor._resume(sid, '@api')
        with api.app.state.db() as db:
            assert db.get(Session, sid).desired_state == 'running'
            if case == 'replacement':
                assert db.get(Setting, 'backup.replacement').value['resume_sessions'] == []
    else:
        assert reason
        expected = {
            'deleted': 'project was deleted', 'explanation': "explanation can't be resumed",
            'external': "didn't start this session", 'private': 'Development workspace',
            'generation': 'custom blocks changed', 'archived': 'session is archived',
            'replacement-stop': "hasn't confirmed", 'team-stop': "hasn't confirmed",
            'multirun-stop': "hasn't confirmed", 'sleep-stop': "hasn't confirmed",
            'team-stopped': 'team is stopped', 'team-expired': 'time limit ended',
            'team-limit': 'as many helpers', 'team-limit-stopping': 'as many helpers', 'assignment-failed': 'attempt was marked failed',
            'assignment-replaced': 'newer attempt replaced', 'cancelled-prepare': 'prepare mode',
            'prepare-write': 'write permission', 'prepare-unassigned': 'discovery assignment',
            'group-budget': 'Your budget pauses', 'group-access': 'no longer have access',
            'group-offline': 'offline', 'group-quota': 'Group launches are paused',
            'group-ceiling': 'most launches an instance owner allows',
        }
        assert expected[case] in reason
        with pytest.raises(LaunchRefused) as refused:
            await supervisor._resume(sid, '@api')
        assert str(refused.value) == reason
        with api.app.state.db() as db:
            after = snapshot(db)
            if case == 'group-quota':
                # The GET/preflight above is entirely read-only. A real quota
                # refusal must persist the base pause, review item and audit.
                assert db.get(Group, 'f' * 32).paused
                operations = list(db.scalars(select(GroupOperation)))
                assert len(operations) == 1 and operations[0].kind == 'quota'
                pauses = list(db.scalars(select(AuditLog).where(AuditLog.action == 'group.quota_pause')))
                assert len(pauses) == 1 and pauses[0].target == 'f' * 32
                assert len(after[AuditLog.__tablename__]) == len(before[AuditLog.__tablename__]) + 1
                changed = {Group.__tablename__, GroupOperation.__tablename__, AuditLog.__tablename__}
                assert {k: v for k, v in after.items() if k not in changed} == {k: v for k, v in before.items() if k not in changed}
            else:
                assert after == before


async def test_resume_check_is_read_only_and_matches_refusal(api, project):
    await pause_supervisor(api)
    made = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude', 'name': 'resume'})
    assert made.status_code == 201, made.text
    sid = made.json()['id']
    with api.app.state.db() as db:
        db.get(Session, sid).desired_state = 'archived'
        db.commit()
        before = snapshot(db)
    checked = await api.get(f'/api/sessions/{sid}/resume-check')
    assert checked.status_code == 200, checked.text
    assert checked.json() == {'eligible': False, 'reason': "This session is archived, so it can't be resumed."}
    resumed = await api.post(f'/api/sessions/{sid}/resume')
    assert resumed.status_code == 409
    assert resumed.json()['detail'] == checked.json()['reason']
    with api.app.state.db() as db:
        assert snapshot(db) == before


async def test_resume_check_member_only_sees_their_session(api, project, member):
    await pause_supervisor(api)
    made = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude', 'name': 'resume'})
    assert made.status_code == 201, made.text
    sid = made.json()['id']
    path = f'/api/sessions/{sid}/resume-check'
    assert (await member.get(path)).status_code == 404
    assert (await member.get('/api/sessions/missing/resume-check')).status_code == 404
    with api.app.state.db() as db:
        session = db.get(Session, sid)
        from acc.server.child_caps import transfer_owner
        transfer_owner(db, [session], 'dana', '@api')
        session.desired_state = 'archived'
        db.commit()
    checked = await member.get(path)
    assert checked.status_code == 200, checked.text
    assert checked.json()['eligible'] is False
    assert 'archived' in checked.json()['reason']


async def test_lost_group_capacity_preserves_replacement_grant_and_cancelled_attempt(api, project, monkeypatch):
    from acc.server import budgets, group_board, group_remote
    from acc.server.orchestrator.assignments import Assignment, Attempt

    await pause_supervisor(api)
    made = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude', 'name': 'Replacement helper'})
    assert made.status_code == 201, made.text
    sid = made.json()['id']
    supervisor = api.app.state.supervisor
    group_id, team_id, assignment_id, attempt_id, node_id = (letter * 32 for letter in 'fbcda')
    monkeypatch.setattr(budgets, 'paused', lambda *a, **kw: [])
    monkeypatch.setattr(group_board, 'authorized', lambda *a: True)
    monkeypatch.setattr(group_remote, 'source_placement', lambda *a: object())
    monkeypatch.setattr(group_remote, 'connected', lambda *a: True)
    with api.app.state.db() as db:
        session = legacy_child(db, db.get(Session, sid))
        sid, team_id = session.id, session.parent_session_id
        # Model an existing pre-Sources replacement session, not a local-to-remote retarget.
        target = db.get(SessionTarget, sid)
        if target is not None:
            db.delete(target)
        session.desired_state = session.observed_state = 'archived'
        session.placement, session.team_host = None, 'example'
        session.team_id = team_id
        db.add(Group(id=group_id, name='Replacement group', created_by='@api', concurrent=2))
        db.add(Team(id=team_id, limits={'at_once': 2}))
        db.add(Node(id=node_id, name='example', status='active', public_key=base64.b64encode(b'public key').decode()))
        session.project.group_id, session.project.owner = group_id, None
        db.add(Setting(key='backup.replacement', value={
            'resume_sessions': [sid], 'reviewed_nodes': {node_id: hashlib.sha256(b'public key').hexdigest()},
            'resume_bindings': {sid: {'node_id': node_id, 'account': session.run_as, 'native_id': session.native_id}}}))
        db.flush()
        db.add(Assignment(id=assignment_id, team_id=team_id, created_by=sid,
                          task_type='implementation', goal='continue', need={}, state='blocked'))
        db.flush()
        db.add(Attempt(id=attempt_id, assignment_id=assignment_id, session_id=sid, sequence=0, state='cancelled'))
        bind_child_proof(db, session)
        db.commit()
        before = snapshot(db)
    # Capacity is available in GET, which must not reserve or change any table.
    checked = await api.get(f'/api/sessions/{sid}/resume-check')
    assert checked.status_code == 200, checked.text
    assert checked.json() == {'eligible': True, 'reason': None}
    with api.app.state.db() as db:
        assert snapshot(db) == before
    real_reserve = group_board.reserve
    at_reservation = []
    def lose_last_slot(db, group, *, consume=True):
        if consume:
            at_reservation.append({
                'grants': list(db.get(Setting, 'backup.replacement').value['resume_sessions']),
                'attempt': db.get(Attempt, attempt_id).state,
                'assignment': db.get(Assignment, assignment_id).state,
            })
            # Simulate losing capacity after preflight. Keep the fenced real
            # reserve (including its commit-on-pause) entirely intact.
            group.concurrent = 0
        return real_reserve(db, group, consume=consume)
    monkeypatch.setattr(group_board, 'reserve', lose_last_slot)
    with pytest.raises(LaunchRefused, match='Group launches are paused'):
        await supervisor._resume(sid, '@api')
    with api.app.state.db() as db:
        assert db.get(Session, sid).desired_state == 'archived'
        assert db.get(Setting, 'backup.replacement').value['resume_sessions'] == [sid]
        assert db.get(Attempt, attempt_id).state == 'cancelled'
        assert db.get(Assignment, assignment_id).state == 'blocked'
        assert db.get(Group, group_id).paused
        assert len(list(db.scalars(select(GroupOperation).where(GroupOperation.group_id == group_id)))) == 1
        assert len(list(db.scalars(select(AuditLog).where(AuditLog.action == 'group.quota_pause')))) == 1
        assert not list(db.scalars(select(AuditLog).where(AuditLog.action.in_(['assignment.attempt_resumed', 'session.resume']))))
        assert not list(db.scalars(select(GroupLaunch)))
        after = snapshot(db)
        changed = {Group.__tablename__, GroupOperation.__tablename__, AuditLog.__tablename__}
        assert {k: v for k, v in after.items() if k not in changed} == {k: v for k, v in before.items() if k not in changed}
    assert at_reservation == [{'grants': [sid], 'attempt': 'cancelled', 'assignment': 'blocked'}]
    # The quota refusal must remain read-only for GET and keep the same message.
    checked = await api.get(f'/api/sessions/{sid}/resume-check')
    assert checked.status_code == 200 and not checked.json()['eligible']
    assert 'Group launches are paused' in checked.json()['reason']
    with api.app.state.db() as db:
        assert snapshot(db) == after
        group = db.get(Group, group_id)
        group.concurrent, group.paused = 2, False  # simulate the maintainer resolving the pause
        db.commit()
    monkeypatch.setattr(group_board, 'reserve', real_reserve)
    await supervisor._resume(sid, '@api')
    with api.app.state.db() as db:
        assert db.get(Session, sid).desired_state == 'running'
        assert db.get(Setting, 'backup.replacement').value['resume_sessions'] == []
        assert db.get(Attempt, attempt_id).state == 'running'
        assert db.get(Assignment, assignment_id).state == 'running'
        assert len(list(db.scalars(select(GroupLaunch)))) == 1
        resumes = list(db.scalars(select(AuditLog).where(AuditLog.action == 'session.resume')))
        assert len(resumes) == 1 and resumes[0].detail == {'replacement_exception': True, 'node': 'example'}
