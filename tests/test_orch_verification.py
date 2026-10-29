"""Lane B regression tests: real scheduler/API state, isolated fixture machines only."""
from datetime import timedelta

import pytest
from helpers import write_db
from test_orchestrator_gates import add
from test_orchestrator_gates import queue as gate_queue
from test_orchestrator_selection_api import assigned_start

queue = gate_queue

from acc.server.models import Session, Setting, Team, utcnow
from acc.server.orchestrator import gates, reservations


@pytest.fixture(autouse=True)
def switches(monkeypatch):
    monkeypatch.setenv('ACC_ORCHESTRATOR', '1')


async def test_stop_cancels_running_and_queued_gates_and_releases_numbers(queue):
    api, project, machine, scheduler = queue
    sup = api.app.state.supervisor
    team_id = 'a' * 32
    stops = []
    original_call = machine.call
    async def broker(method, **params):
        if method == 'gate_stop':
            stops.append(params)
            machine.finished.add(params['job'])
        return await original_call(method, **params)
    sup.broker.call = broker
    with write_db(sup.db) as db:
        db.add(Team(id=team_id, limits={}, expires_at=utcnow() + timedelta(hours=1)))
        db.add(reservations.Reservation(kind='migration', number=99, team_id=team_id,
                                       state='reserved', expires_at=utcnow() + timedelta(days=1)))
        db.commit()
    running = add(api, project, 'acc/running', team_id=team_id)
    await scheduler.tick()
    queued = add(api, project, 'acc/queued', team_id=team_id)
    await sup.stop_team(team_id, 'casey')
    with sup.db() as db:
        assert db.get(gates.GateJob, running).waiting == 'cancel'
        assert db.get(gates.GateJob, queued).state == 'cancelled'
        assert db.get(reservations.Reservation, 1).state == 'released'
    await scheduler.tick()
    assert stops and stops[0]['job'] == running
    assert (stops[0]['pid'], stops[0]['start']) == (4242, '1')
    with sup.db() as db:
        for job in (running, queued):
            row = db.get(gates.GateJob, job)
            assert row.state == 'cancelled' and row.reason == 'cancelled: team stopped'


@pytest.mark.parametrize('change,reason', [('branch', 'branch moved'), ('base', 'base moved'), ('commands', 'commands changed')])
async def test_gate_freshness_binds_head_base_and_commands(queue, change, reason):
    api, project, machine, scheduler = queue
    job = add(api, project, 'acc/verified')
    await scheduler.tick()
    machine.finished.add(job)
    await scheduler.tick()
    if change == 'commands':
        with write_db(api.app.state.db) as db:
            db.get(Setting, gates.COMMANDS_KEY).value = {str(project['id']): {'full': ['changed', '{junit}'], 'related': ['changed', '{junit}']}}
            db.commit()
    else:
        machine.heads = {'acc/verified' if change == 'branch' else 'main': 'e' * 40}
    rows = (await api.get('/api/gates')).json()['jobs']
    result = next(j for j in rows if j['id'] == job)
    assert result['stale'] == reason
    assert result['base_sha'] and result['commands_digest'] and result['runner_version']


async def test_new_retry_pass_is_not_an_integration_pass(queue):
    api, project, machine, scheduler = queue
    job = add(api, project, 'acc/retry')
    await scheduler.tick()
    machine.results[job] = {'exit': 1, 'failed': ['tests/test_x.py::test_one'], 'rerunnable': True}
    machine.finished.add(job)
    await scheduler.tick()
    machine.results[job] = {'exit': 0, 'failed': []}
    await scheduler.tick()
    with api.app.state.db() as db:
        row = db.get(gates.GateJob, job)
        assert row.state == 'retry-passed' and row.state not in gates.PASSED
        assert row.results['exit'] == 1 and row.results['rerun']['exit'] == 0


async def test_owner_flake_list_classifies_only_all_known_failures(queue):
    api, project, machine, scheduler = queue
    response = await api.put(f"/api/gates/projects/{project['id']}/flakes", json={'tests': ['tests/test_x.py::test_one']})
    assert response.status_code == 200
    job = add(api, project, 'acc/known')
    await scheduler.tick()
    machine.results[job] = {'exit': 1, 'failed': ['tests/test_x.py::test_one'], 'rerunnable': True}
    machine.finished.add(job)
    await scheduler.tick()
    machine.results[job] = {'exit': 0, 'failed': []}
    await scheduler.tick()
    with api.app.state.db() as db:
        assert db.get(gates.GateJob, job).state == 'known-flake'


def test_stop_states_do_not_call_an_offline_request_confirmed():
    from acc.server.orchestrator import verification
    row = Session(id='a' * 32, desired_state='stopped', observed_state='idle', team_stop_pending=True)
    assert verification.stop_state(row) == 'stop requested'
    row.team_stop_pending = False
    assert verification.stop_state(row) == 'stop confirmed'
    row.desired_state = 'archived'
    assert verification.stop_state(row) == 'finished'


def test_freshness_reads_main_tip_through_real_broker(tmp_path):
    from test_changes import git, repo
    from test_orchestrator_gates import config, git_out

    from acc.broker import gate_ops
    root = tmp_path / 'repo'
    repo(root)
    git(root, 'checkout', '-qb', 'acc/check')
    result = gate_ops.gate_heads(config(tmp_path), str(root), ['main', 'acc/check'])
    assert result['heads']['main'] == git_out(root, 'rev-parse', 'main')


@pytest.mark.parametrize('minutes', [0, 30, 120])
async def test_expiry_stops_helpers_then_lead_at_original_wrapup_deadline(api, project, env, minutes):
    from helpers import pause_supervisor
    from test_team import lead

    from acc.server.orchestrator import verification
    owner = await lead(api, project, env)
    helper = (await assigned_start(api, env, owner, name='Worker', task='Work'))['result']
    sup = api.app.state.supervisor
    await pause_supervisor(api)
    at = utcnow()
    with write_db(sup.db) as db:
        db.merge(Setting(key=verification.WRAPUP_KEY, value=minutes))
        db.get(Team, owner['id']).expires_at = at - timedelta(seconds=1)
        db.commit()
    await sup.expire_teams()
    with sup.db() as db:
        team = db.get(Team, owner['id'])
        caller = db.get(Session, owner['id'])
        assert team.wrapup_until == team.expires_at + timedelta(minutes=minutes)
        assert caller.desired_state == ('running' if minutes else 'stopped')
        assert db.get(Session, helper['id']).desired_state == 'stopped'
        if not minutes:
            return
        for action in verification.WRAPUP_ACTIONS:
            assert verification.allowed(team, caller, action)
        for action in ('start', 'merge', 'gate', 'reserve', 'select', 'message', 'plan_ready', 'now', 'list'):
            assert not verification.allowed(team, caller, action)
        assert not verification.allowed(team, caller, 'report', team.wrapup_until)
    with write_db(sup.db) as db:
        db.get(Team, owner['id']).wrapup_until = at - timedelta(seconds=1)
        db.commit()
    await sup.expire_teams()
    with sup.db() as db:
        assert db.get(Session, owner['id']).desired_state == 'stopped'


@pytest.mark.parametrize('code,headers,expected', [
    (404, {}, 'Repository not found'), (429, {}, 'rate limited'),
    (403, {'X-RateLimit-Remaining': '0'}, 'rate limited'),
    (403, {}, 'refused access'), (401, {}, 'refused access'),
])
def test_updates_distinguish_private_repo_from_rate_limit(monkeypatch, code, headers, expected):
    import urllib.error
    import urllib.request
    from email.message import Message

    from acc.updates import fetch
    message = Message()
    for key, value in headers.items():
        message[key] = value
    class Opener:
        def open(self, *args, **kwargs):
            raise urllib.error.HTTPError('https://example.com/private-token', code, 'SECRET', message, None)
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *args: Opener())
    with pytest.raises(ValueError, match=expected) as error:
        fetch('https://example.com/release')
    assert 'SECRET' not in str(error.value) and 'private-token' not in str(error.value)


def test_review_binding_requires_both_target_branch_and_sha():
    from acc.server.orchestrator import verification
    assert verification.review_fields({'reviewed_branch': 'acc/author', 'reviewed_head': 'a' * 40}) == {
        'reviewed_branch': 'acc/author', 'reviewed_head': 'a' * 40}
    for payload in ({'reviewed_head': 'a' * 40}, {'reviewed_branch': 'main', 'reviewed_head': 'a' * 40},
                    {'reviewed_branch': 'acc/author', 'reviewed_head': '../HEAD'}):
        with pytest.raises(ValueError):
            verification.review_fields(payload)


def review_task(sup, lead, reviewer, author, task_type='review'):
    """The reviewer's selection, as select records it for a review of `author`."""
    import uuid

    from acc.server.orchestrator.assignments import current
    from acc.server.orchestrator.selection import Selection
    with sup.db() as db:
        attempt = current(db, reviewer['id'])
        if attempt:
            db.get(Selection, attempt.selection_id).need = {'task_type': task_type, 'author_session': author['id']}
            db.commit()
            return
        db.add(Selection(id=uuid.uuid4().hex, team_id=lead['id'], requested_by=lead['id'], request_id=uuid.uuid4().hex,
                         need={'task_type': task_type, 'author_session': author['id']}, task_digest='0' * 64,
                         candidates=[], chosen=0, reason='test', budget={}, state='started', session_id=reviewer['id']))
        db.commit()


async def test_review_report_is_bound_to_author_head_and_survives_author_changes(api, project, env, monkeypatch):
    from helpers import wait_for
    from test_orchestrator_delivery import idle
    from test_team import lead, request

    from acc.server.orchestrator import delivery, verification
    monkeypatch.setenv('ACC_ORCHESTRATOR_V2_DELIVERY', '1')
    owner = await lead(api, project, env)
    writer = (await assigned_start(api, env, owner, name='Writer', task='Write'))['result']
    reviewer = (await assigned_start(api, env, owner, name='Reviewer', task='Review only'))['result']
    await wait_for(lambda: idle(api, reviewer['id']))
    sup = api.app.state.supervisor
    with sup.db() as db:
        author = db.get(Session, writer['id'])
        branch = author.worktree_branch
        before = await verification.evidence(sup, author)
    head = before['review']['head']
    assert head
    review_task(sup, owner, reviewer, writer)
    response = await request(api, env, reviewer, 'report', status='done', summary='Reviewed the author revision',
                             reviewed_branch=branch, reviewed_head=head)
    assert response['ok'], response
    with sup.db() as db:
        report = db.get(delivery.TaskReport, (reviewer['id'], response['result']['task_id']))
        assert report.reviewed_head == head and report.reviewed_branch == branch
        assert verification.review_evidence([report], head)['status'] == 'reviewed at this revision'
        assert verification.review_evidence([report], 'f' * 40)['status'] == 'not reviewed at this revision'
    await delivery.stop_worker(sup)


async def test_known_flake_does_not_hide_an_unlisted_failure(queue):
    api, project, machine, scheduler = queue
    await api.put(f"/api/gates/projects/{project['id']}/flakes", json={'tests': ['tests/test_x.py::test_one']})
    job = add(api, project, 'acc/mixed')
    await scheduler.tick()
    machine.finished.add(job)
    machine.results[job] = {'exit': 1, 'failed': ['tests/test_x.py::test_one', 'tests/test_x.py::test_new'], 'rerunnable': True}
    await scheduler.tick()
    machine.results[job] = {'exit': 0, 'failed': []}
    await scheduler.tick()
    with api.app.state.db() as db:
        assert db.get(gates.GateJob, job).state == 'retry-passed'
        assert not gates.integration_pass(db, db.get(gates.GateJob, job))


async def test_stop_wins_gate_start_wait_and_the_record_is_kept(queue):
    api, project, machine, scheduler = queue
    sup = api.app.state.supervisor
    team_id = 'a' * 32
    with write_db(sup.db) as db:
        db.add(Team(id=team_id, limits={}, expires_at=utcnow() + timedelta(hours=1)))
        db.commit()
    original = machine.call
    async def racing(method, **params):
        if method == 'gate_start':
            await sup.stop_team(team_id, 'owner')
        return await original(method, **params)
    sup.broker.call = racing
    job = add(api, project, 'acc/start-race', team_id=team_id)
    await scheduler.tick()
    with sup.db() as db:
        row = db.get(gates.GateJob, job)
        assert row.waiting == 'cancel' and row.pid_record['pid'] == 4242
    machine.finished.add(job)
    await scheduler.tick()
    with sup.db() as db:
        assert db.get(gates.GateJob, job).reason == 'cancelled: team stopped'


@pytest.mark.parametrize('minutes,status', [(0, 200), (120, 200), (-1, 422), (121, 422), (True, 422), (1.5, 422)])
async def test_wrapup_setting_bounds(api, minutes, status):
    assert (await api.put('/api/gates/wrapup', json={'minutes': minutes})).status_code == status


async def test_retry_acceptance_requires_an_owner_and_a_flake_task_in_the_same_team(queue):
    api, project, machine, scheduler = queue
    from acc.server.orchestrator.worklist import WorkItem
    team_id = 'a' * 32
    with write_db(api.app.state.db) as db:
        db.add(Team(id=team_id, limits={}, expires_at=utcnow() + timedelta(hours=1)))
        db.flush()
        item = WorkItem(team_id=team_id, position=1, kind='task', title='Investigate flake', source='lead', links=[])
        db.add(item)
        db.commit()
        item_id = item.id
    job = add(api, project, 'acc/accept', team_id=team_id)
    with write_db(api.app.state.db) as db:
        row = db.get(gates.GateJob, job)
        row.state, row.results = 'retry-passed', {'failed': ['tests/test_x.py::test_one'], 'exit': 1}
        db.commit()
    assert (await api.post(f'/api/gates/{job}/accept-retry', json={'item': item_id + 1000})).status_code == 409
    response = await api.post(f'/api/gates/{job}/accept-retry', json={'item': item_id})
    assert response.status_code == 200
    with api.app.state.db() as db:
        row = db.get(gates.GateJob, job)
        assert row.state == 'retry-passed' and row.results['exit'] == 1
        assert gates.integration_pass(db, row)


def test_integration_refuses_a_head_changed_after_review(tmp_path):
    from test_changes import git
    from test_orchestrator_gates import config, stacked

    from acc.broker import ops
    from acc.broker.ops import OpError
    root, helper = stacked(tmp_path)
    git(root, 'checkout', '-q', 'acc/team-lead')
    with pytest.raises(OpError, match='head changed'):
        ops.team_merge(config(tmp_path), str(root), 'acc/team-helper', 'acc/team-lead', expected_head='f' * 40)


async def test_member_cannot_change_verification_policy(api, env, member_accounts):
    from test_auth import do_setup
    from test_users import browser, csrf, join
    await do_setup(api, env)
    link = (await api.post('/api/users/invites', json={'role': 'member', 'account': 'dana-agent'})).json()['link']
    async with browser(api) as member:
        assert (await join(member, link, 'alex')).status_code == 200
        for method, url, body in [
            ('PUT', '/api/gates/wrapup', {'minutes': 0}),
            ('PUT', '/api/gates/projects/1/flakes', {'tests': []}),
            ('POST', '/api/gates/' + 'a' * 32 + '/accept-retry', {'item': 1}),
        ]:
            response = await member.request(method, url, json=body, headers=csrf(member))
            assert response.status_code == 403


def test_retry_refuses_to_discard_unarchivable_original_output(tmp_path):
    from acc.broker import gate_ops
    from acc.broker.ops import OpError
    folder = tmp_path / 'job'
    folder.mkdir()
    private = tmp_path / 'private'
    private.write_text('private')
    (folder / 'junit.xml').symlink_to(private)
    with pytest.raises(OpError):
        gate_ops._keep_first_result(folder)
    assert private.read_text() == 'private'


def test_earlier_failure_archive_is_never_overwritten(tmp_path):
    from acc.broker import gate_ops
    (tmp_path / 'junit.xml').write_text('later')
    (tmp_path / 'first-junit.xml').write_text('first failure')
    gate_ops._keep_first_result(tmp_path)
    assert (tmp_path / 'first-junit.xml').read_text() == 'first failure'


async def test_lead_integration_view_uses_same_freshness_and_retry_rules(queue):
    from types import SimpleNamespace

    from acc.server.orchestrator import verification
    api, project, machine, scheduler = queue
    member = SimpleNamespace(project_id=project['id'], worktree_branch='acc/evidence', team_id=None,
                             run_as='agent', host='local', team_repo=None)
    job = add(api, project, member.worktree_branch)
    await scheduler.tick()
    machine.finished.add(job)
    await scheduler.tick()
    sup = api.app.state.supervisor
    assert (await verification.evidence(sup, member))['blocking'] is None
    machine.heads = {'main': 'e' * 40}
    assert (await verification.evidence(sup, member))['blocking'] == 'base moved'
    machine.heads = {}
    with write_db(sup.db) as db:
        db.get(gates.GateJob, job).state = 'retry-passed'
        db.commit()
    assert (await verification.evidence(sup, member))['blocking'] == 'gate retry-passed'
    machine.heads = None
    with write_db(sup.db) as db:
        db.get(gates.GateJob, job).state = 'passed'
        db.commit()
    assert (await verification.evidence(sup, member))['blocking'] == 'gate freshness unconfirmed'


def test_migration_bounds_already_expired_running_leads_and_reclassifies_legacy_retries(tmp_path):
    from datetime import datetime

    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from test_migration_portability import revision

    engine = sa.create_engine('sqlite:///' + str(tmp_path / 'migration.db'))
    at = datetime(2026, 10, 27, 12)
    with engine.begin() as conn:
        conn.exec_driver_sql('CREATE TABLE team (id TEXT PRIMARY KEY, stopped BOOLEAN, expires_at DATETIME)')
        conn.exec_driver_sql('CREATE TABLE session (id TEXT PRIMARY KEY, desired_state TEXT)')
        conn.exec_driver_sql('CREATE TABLE gate_job (id TEXT PRIMARY KEY, state TEXT)')
        conn.exec_driver_sql('CREATE TABLE task_report (id TEXT PRIMARY KEY)')
        for name, desired in [('expired', 'running'), ('stopped', 'stopped')]:
            conn.execute(sa.text('INSERT INTO team VALUES (:id, 1, :at)'), {'id': name, 'at': at.isoformat(sep=' ')})
            conn.execute(sa.text('INSERT INTO session VALUES (:id, :desired)'), {'id': name, 'desired': desired})
        conn.exec_driver_sql("INSERT INTO gate_job VALUES ('legacy', 'flaky-passed')")
        with Operations.context(MigrationContext.configure(conn)):
            revision('0074').upgrade()
        rows = dict(conn.exec_driver_sql('SELECT id, wrapup_until FROM team').fetchall())
        assert datetime.fromisoformat(rows['expired']) == at + timedelta(minutes=30)
        assert rows['stopped'] is None
        assert conn.exec_driver_sql('SELECT state FROM gate_job').scalar() == 'retry-passed'
    engine.dispose()


def test_a_new_review_report_version_supersedes_the_old_verdict():
    from types import SimpleNamespace

    from acc.server.orchestrator.verification import review_evidence
    old = SimpleNamespace(attempt_id='a' * 32, version=1, session_id='reviewer',
                          reviewed_branch='acc/author', reviewed_head='b' * 40, status='done')
    revised = SimpleNamespace(attempt_id='a' * 32, version=2, session_id='reviewer',
                              reviewed_branch='acc/author', reviewed_head='b' * 40, status='partial')
    assert review_evidence([old, revised], 'b' * 40)['status'] == 'not reviewed at this revision'


@pytest.mark.parametrize('status,expected', [(404, 'Repository not found'), (429, 'rate limited')])
async def test_updates_panel_keeps_the_safe_fetch_classification(api, monkeypatch, status, expected):
    import urllib.error
    import urllib.request
    class Opener:
        def open(self, *args, **kwargs):
            raise urllib.error.HTTPError('https://example.com/PRIVATE', status, 'TOKEN', {}, None)
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *args: Opener())
    response = await api.post('/api/updates/check')
    assert response.status_code == 200
    error = response.json()['error']
    assert expected in error and 'Release check failed or' not in error
    assert 'TOKEN' not in error and 'PRIVATE' not in error


async def test_unclassified_updater_errors_still_hide_private_exception_text(api, monkeypatch):
    from acc.server import updating
    def failure(*args):
        raise ValueError('SECRET https://example.com/PRIVATE /private/token-file')
    monkeypatch.setattr(updating, 'check', failure)
    response = await api.post('/api/updates/check')
    message = response.json()['error']
    assert message == 'Release check failed. Check the repository and token file, then try again.'
    assert 'SECRET' not in message and '/private' not in message


def test_gate_refuses_a_machine_without_recordable_process_identity(tmp_path, monkeypatch):
    import sys

    from test_changes import git, repo
    from test_orchestrator_gates import config

    from acc.broker import gate_ops
    from acc.broker.ops import OpError
    cfg, root, job = config(tmp_path), tmp_path / 'repo', 'e' * 32
    repo(root)
    git(root, 'checkout', '-qb', 'acc/identity')
    gate_ops.gate_prepare(cfg, str(root), job, 'acc/identity', 'main')
    monkeypatch.setattr(gate_ops, '_start_time', lambda pid: None)
    try:
        with pytest.raises(OpError, match='process identity'):
            gate_ops.gate_start(cfg, job, [sys.executable, '-c', 'pass'])
    finally:
        child = gate_ops._children.pop(job, None)
        if child is not None:
            child.wait(timeout=10)  # only the tiny fixture child started above
        gate_ops.gate_cleanup(cfg, job)


async def test_only_a_review_task_for_that_author_can_certify_a_revision(api, project, env, monkeypatch):
    from helpers import wait_for
    from test_orchestrator_delivery import idle
    from test_team import lead, request

    from acc.server.orchestrator import delivery, verification
    monkeypatch.setenv('ACC_ORCHESTRATOR_V2_DELIVERY', '1')
    owner = await lead(api, project, env)
    writer = (await assigned_start(api, env, owner, name='Writer', task='Write'))['result']
    other = (await assigned_start(api, env, owner, name='Other', task='Write more'))['result']
    sibling = (await assigned_start(api, env, owner, name='Sibling', task='Implement'))['result']
    await wait_for(lambda: idle(api, sibling['id']))
    sup = api.app.state.supervisor
    with sup.db() as db:
        author = db.get(Session, writer['id'])
        branch, head = author.worktree_branch, (await verification.evidence(sup, author))['review']['head']
    claim = dict(status='done', summary='Looks reviewed', reviewed_branch=branch, reviewed_head=head)
    # An implementer, and the lead, hold no review task.
    for caller in (sibling, owner):
        refused = await request(api, env, caller, 'report', **claim)
        assert not refused['ok'] and 'only a review task' in refused['error'], refused
    # A review task for a different author can't certify this branch.
    review_task(sup, owner, sibling, other)
    refused = await request(api, env, sibling, 'report', **claim)
    assert not refused['ok'] and "author's branch named in your review task" in refused['error'], refused
    await delivery.stop_worker(sup)


@pytest.mark.parametrize('gate,status,blocking,expected', [
    (None, 'not reviewed at this revision', None, None),
    ({'state': 'passed'}, 'not reviewed at this revision', None, True),
    ({'state': 'passed'}, 'not reviewed at this revision', 'branch moved', False),
    (None, 'reviewed at this revision', None, True),
])
def test_assignment_verification_uses_the_merge_evidence_snapshot(gate, status, blocking, expected):
    from acc.server.orchestrator.assignments import verification_result
    snapshot = {'gate': gate, 'review': {'status': status, 'head': 'a' * 40}, 'blocking': blocking}
    result = verification_result(snapshot)
    assert result == (None if expected is None else {'verified': expected, 'head': 'a' * 40})
