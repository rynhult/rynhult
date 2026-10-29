"""Authority changes at v2's broker waits, using only fixture brokers and databases."""
import pytest
from helpers import pause_supervisor, write_db
from sqlalchemy import func, select
from test_orchestrator_delivery import v2  # noqa: F401
from test_orchestrator_gates import add, jobs, queue  # noqa: F401
from test_orchestrator_selection_api import assigned_start, human, proposal, selection_runtime  # noqa: F401
from test_team import lead, request

from acc.server.models import AuditLog, Session, Setting, Team
from acc.server.orchestrator import gates, reservations, selection
from acc.server.supervisor_common import LaunchRefused


@pytest.mark.parametrize('change', ['queue_off', 'preview_off', 'team_stopped', 'caller_stopped'])
async def test_reservation_rechecks_authority_after_broker_scan(api, env, project, monkeypatch, change):
    caller = await lead(api, project, env)
    await pause_supervisor(api)
    supervisor = api.app.state.supervisor
    with write_db(supervisor.db) as db:
        db.merge(Setting(key=gates.BUDGET_KEY, value={**gates.DEFAULT_BUDGET, 'enabled': True}))
        db.commit()

    async def scan(method, **params):
        assert method == 'gate_numbers'
        if change == 'preview_off':
            monkeypatch.setenv('ACC_ORCHESTRATOR', '0')
        else:
            with write_db(supervisor.db) as db:
                if change == 'queue_off':
                    db.get(Setting, gates.BUDGET_KEY).value = {**gates.DEFAULT_BUDGET, 'enabled': False}
                elif change == 'team_stopped':
                    db.get(Team, caller['id']).stopped = True
                else:
                    db.get(Session, caller['id']).desired_state = 'stopped'
                db.commit()
        return {'base': {}, 'pending': {}}

    monkeypatch.setattr(supervisor.broker, 'call', scan)
    with pytest.raises(LaunchRefused, match='off|stopped'):
        await gates.reserve_tool(supervisor, caller['id'], {'kind': 'migration'})
    with supervisor.db() as db:
        assert db.scalar(select(func.count()).select_from(reservations.Reservation)) == 0
        assert db.scalar(select(AuditLog).where(AuditLog.action == 'reservation.reserve')) is None


@pytest.mark.parametrize('change', ['owner', 'grant_removed', 'machine_removed'])
async def test_selected_start_refuses_changed_frozen_authority(api, env, project, change):
    caller, row = await proposal(api, env, project)
    with write_db(api.app.state.db) as db:
        team = db.get(Team, caller['id'])
        if change == 'owner':
            db.get(Session, caller['id']).owner = 'another-person'
        elif change == 'grant_removed':
            team.limits = {**team.limits, 'agents': []}
        else:
            team.limits = {**team.limits, 'machines': []}
        db.commit()
    reply = await request(api, env, caller, 'start', assignment=row['assignment_id'], selection=row['id'], name='Refused')
    assert not reply['ok'], reply
    with api.app.state.db() as db:
        assert db.get(Team, caller['id']).started == 0
        # M17 rejects a removed agent/machine before claiming the selection; an owner change
        # reaches the claim's independent recheck and records a selection refusal. Neither launches.
        assert db.get(selection.Selection, row['id']).state == ('refused' if change == 'owner' else 'proposed')
        assert db.scalar(select(AuditLog).where(AuditLog.action == 'team.start')) is None


async def test_person_override_rechecks_allowlist_after_readiness(api, env, project, monkeypatch):
    headers = await human(api, env)
    caller, row = await proposal(api, env, project)
    before = row['chosen']
    allowed = {'version': 1, 'rules': [{'profile': '*', 'task_type': 'implementation', 'choices': []}]}
    ready = selection.readiness

    async def narrowed(*args):
        result = await ready(*args)
        # A second owner's transaction lands while the first tab waits for the broker.
        with write_db(api.app.state.db) as db:
            db.merge(Setting(key=selection.POLICY_KEY, value=allowed))
            db.commit()
        return result

    monkeypatch.setattr(selection, 'readiness', narrowed)
    response = await api.post(f"/api/teams/{caller['id']}/selections/{row['id']}/override",
                              headers=headers, json={'choice': 1, 'reason': 'Stale tab'})
    assert response.status_code == 409, response.text
    with api.app.state.db() as db:
        ledger = db.get(selection.Selection, row['id'])
        assert ledger.state == 'proposed' and ledger.chosen == before
        assert ledger.override_by is None
        assert db.get(Team, caller['id']).started == 0
        assert db.scalar(select(AuditLog).where(AuditLog.action == 'selection.overridden')) is None


async def test_other_member_cannot_read_or_override_team_or_edit_owner_policy(api, env, project, member_accounts, monkeypatch):
    from helpers import verified_post
    from test_users import browser, csrf, join

    from acc.server.orchestrator import delivery

    await human(api, env)
    caller, row = await proposal(api, env, project)
    await pause_supervisor(api)
    monkeypatch.setenv('ACC_ORCHESTRATOR_V2_DELIVERY', '1')
    message = delivery.enqueue(api.app.state.supervisor, caller['id'], '@api', 'Private owner instruction')
    assert (await api.get('/api/deliveries/' + 'f' * 32)).status_code == 404
    invitation = await verified_post(api, '/api/users/invites', {'role': 'member', 'account': 'dana-agent'})
    assert invitation.status_code == 201, invitation.text
    async with browser(api) as member:
        assert (await join(member, invitation.json()['link'], 'alex')).status_code == 200
        headers = csrf(member)
        for method, path, body, status in [
            ('GET', '/api/settings/orchestrator/selection', None, 403),
            ('PUT', '/api/settings/orchestrator/selection', {'version': 1, 'rules': []}, 403),
            ('GET', f"/api/teams/{caller['id']}/selections", None, 404),
            ('POST', f"/api/teams/{caller['id']}/selections/{row['id']}/override",
             {'choice': 1, 'reason': 'I am the owner'}, 404),
            ('GET', f"/api/gates/projects/{project['id']}/commands", None, 403),
            ('GET', f"/api/gates?project={project['id']}", None, 404),
            ('GET', f"/api/deliveries/{message['id']}", None, 404),
            ('GET', f"/api/sessions/{caller['id']}/report", None, 404),
        ]:
            response = await member.request(method, path, headers=headers, **({'json': body} if body else {}))
            assert response.status_code == status, (path, response.text)
            assert 'Private owner instruction' not in response.text
    with api.app.state.db() as db:
        ledger = db.get(selection.Selection, row['id'])
        assert ledger.state == 'proposed' and ledger.override_by is None
        assert db.get(Setting, selection.POLICY_KEY) is None


@pytest.mark.parametrize('action,extra', [
    ('select', {'need': {'task_type': 'implementation'}, 'task': 'work', 'owner': 'owner'}),
    ('gate', {'suite': 'full', 'requested_by': 'owner'}),
    ('reserve', {'kind': 'migration', 'confirmed': True}),
    ('start', {'selection': 'a' * 32, 'name': 'Forged', 'task': 'work', 'override_by': 'owner'}),
])
async def test_lead_cannot_supply_person_authority_in_spool(api, env, project, action, extra):
    caller = await lead(api, project, env)
    reply = await request(api, env, caller, action, **extra)
    assert reply == {'ok': False, 'error': 'invalid team action or arguments'}
    with api.app.state.db() as db:
        assert db.get(Team, caller['id']).started == 0
        assert db.scalar(select(func.count()).select_from(selection.Selection)) == 0
        assert db.scalar(select(func.count()).select_from(gates.GateJob)) == 0
        assert db.scalar(select(func.count()).select_from(reservations.Reservation)) == 0


@pytest.mark.parametrize('change', ['queue_off', 'preview_off', 'commands_removed', 'commands_changed'])
async def test_gate_does_not_launch_with_authority_revoked_during_prepare(queue, monkeypatch, change):  # noqa: F811
    api, project, machine, scheduler = queue
    job = add(api, project, 'acc/team-authority')
    call = machine.call
    cleaned = []

    async def interleaved(method, **params):
        result = await call(method, **params)
        if method == 'gate_cleanup':
            cleaned.append(params['job'])
        if method == 'gate_prepare':
            if change == 'preview_off':
                monkeypatch.setenv('ACC_ORCHESTRATOR', '0')
            else:
                with write_db(api.app.state.db) as db:
                    if change == 'queue_off':
                        db.get(Setting, gates.BUDGET_KEY).value = {**gates.DEFAULT_BUDGET, 'enabled': False}
                    elif change == 'commands_removed':
                        db.get(Setting, gates.COMMANDS_KEY).value = {}
                    else:
                        db.get(Setting, gates.COMMANDS_KEY).value = {str(project['id']): {
                            'full': ['new-full', '{junit}'], 'related': ['new-related', '{junit}']}}
                    db.commit()
        return result

    monkeypatch.setattr(api.app.state.supervisor.broker, 'call', interleaved)
    await scheduler.tick()
    assert machine.started == []  # owner consent at enqueue/prepare cannot authorize a later launch
    assert jobs(api)[job][0] != 'running'
    assert cleaned == [job]  # no abandoned prepared checkout
    with api.app.state.db() as db:
        assert db.scalar(select(AuditLog).where(AuditLog.action == 'gate.start', AuditLog.target == job)) is None


@pytest.mark.parametrize('change', ['demoted', 'removed'])
async def test_queued_delivery_rechecks_person_role_before_any_broker_call(v2, env, project, monkeypatch, change):  # noqa: F811
    from acc.server.models import User
    from acc.server.orchestrator import delivery

    caller = await lead(v2, project, env)
    await pause_supervisor(v2)
    supervisor = v2.app.state.supervisor
    with write_db(supervisor.db) as db:
        target = db.get(Session, caller['id'])
        target.owner, target.observed_state = 'another-person', 'idle'
        db.add(User(username='queue-owner', role='owner', password_hash='unused', totp_secret='unused'))
        db.commit()
    message = delivery.enqueue(supervisor, caller['id'], 'queue-owner', 'Owner-only queued instruction')
    with write_db(supervisor.db) as db:
        assert delivery.authorized(db, db.get(Session, caller['id']), 'queue-owner')
        person = db.scalar(select(User).where(User.username == 'queue-owner'))
        if change == 'demoted':
            person.role = 'member'
        else:
            db.delete(person)
        db.commit()

    async def forbidden(*args, **kwargs):
        pytest.fail('revoked person authority reached the broker')

    monkeypatch.setattr(supervisor.broker, 'call', forbidden)
    await delivery.process_once(supervisor)
    with supervisor.db() as db:
        row = db.get(delivery.MessageDelivery, message['id'])
        assert (row.state, row.error, row.body, row.attempts) == ('failed', 'authority_revoked', '', 0)
        audit = db.scalar(select(AuditLog).where(AuditLog.action == 'delivery.failed'))
        assert audit.actor == 'queue-owner' and audit.detail['error'] == 'authority_revoked'


@pytest.mark.parametrize('action,payload', [
    ('team_status', {'helper': 'a' * 32}),
    ('team_message', {'helper': 'a' * 32, 'text': 'A normal instruction'}),
    ('team_report', {'status': 'done', 'summary': 'A normal report'}),
])
async def test_delivery_tools_refuse_master_off_even_with_slice_on(v2, env, project, monkeypatch, action, payload):  # noqa: F811
    from acc.server.orchestrator import delivery

    caller = await lead(v2, project, env)
    await pause_supervisor(v2)
    monkeypatch.setenv('ACC_ORCHESTRATOR', '0')
    assert (await v2.get(f"/api/sessions/{caller['id']}/report")).status_code == 404
    assert (await v2.get('/api/deliveries/' + 'a' * 32)).status_code == 404
    with pytest.raises(LaunchRefused, match='disabled'):
        await getattr(delivery, action)(v2.app.state.supervisor, caller['id'], payload)
    with v2.app.state.db() as db:
        assert db.scalar(select(func.count()).select_from(delivery.MessageDelivery)) == 0
        assert db.scalar(select(func.count()).select_from(delivery.TaskReport)) == 0


async def test_delivery_tools_keep_sibling_outside_subtree_and_stopped_lead_out(v2, env, project):  # noqa: F811
    from acc.server.orchestrator import delivery

    caller = await lead(v2, project, env)
    first = (await assigned_start(v2, env, caller, name='First', task='work'))['result']
    second = (await assigned_start(v2, env, caller, name='Second', task='work'))['result']
    for action, fields in [('status', {}), ('message', {'text': 'Not my helper'})]:
        response = await request(v2, env, first, action, helper=second['id'], **fields)
        assert not response['ok'] and 'outside caller subtree' in response['error']
    response = await request(v2, env, caller, 'status', helper=second['id'])
    assert response['ok'] and response['result']['deliveries'] == [] and response['result']['report'] is None
    await pause_supervisor(v2)
    with write_db(v2.app.state.db) as db:
        db.get(Session, caller['id']).desired_state = 'stopped'
        db.commit()
    with pytest.raises(LaunchRefused, match='caller is not running'):
        await delivery.team_status(v2.app.state.supervisor, caller['id'], {'helper': second['id']})
    with v2.app.state.db() as db:
        assert db.scalar(select(func.count()).select_from(delivery.MessageDelivery)) == 0


@pytest.mark.parametrize('change', ['queue_off', 'preview_off'])
async def test_failed_gate_does_not_start_targeted_rerun_after_switch_off(queue, monkeypatch, change):  # noqa: F811
    api, project, machine, scheduler = queue
    job = add(api, project, 'acc/team-rerun-authority')
    await scheduler.tick()
    assert len(machine.started) == 1
    machine.finished.add(job)
    machine.results[job] = {'exit': 1, 'counts': {'tests': 1, 'failed': 1},
                            'failed': ['tests/test_example.py::test_one'], 'rerunnable': True}
    call = machine.call

    async def interleaved(method, **params):
        result = await call(method, **params)
        if method == 'gate_status':
            if change == 'preview_off':
                monkeypatch.setenv('ACC_ORCHESTRATOR', '0')
            else:
                with write_db(api.app.state.db) as db:
                    db.get(Setting, gates.BUDGET_KEY).value = {**gates.DEFAULT_BUDGET, 'enabled': False}
                    db.commit()
        return result

    monkeypatch.setattr(api.app.state.supervisor.broker, 'call', interleaved)
    await scheduler.tick()
    assert len(machine.started) == 1
    with api.app.state.db() as db:
        row = db.get(gates.GateJob, job)
        assert row.state == 'failed' and 'off' in row.reason
        assert row.results['failed'] == ['tests/test_example.py::test_one']


async def test_queued_gate_with_removed_commands_is_refused_before_preparing(queue, monkeypatch):  # noqa: F811
    api, project, machine, scheduler = queue
    job = add(api, project, 'acc/team-no-commands')
    with write_db(api.app.state.db) as db:
        db.get(Setting, gates.COMMANDS_KEY).value = {}
        db.commit()

    calls = []
    call = machine.call

    async def tracked(method, **params):
        calls.append(method)
        return await call(method, **params)

    monkeypatch.setattr(api.app.state.supervisor.broker, 'call', tracked)
    await scheduler.tick()
    assert calls == []
    assert machine.started == []
    with api.app.state.db() as db:
        row = db.get(gates.GateJob, job)
        assert row.state == 'refused' and 'commands' in row.reason
        assert db.scalar(select(AuditLog).where(AuditLog.action == 'gate.start', AuditLog.target == job)) is None


async def test_owner_can_resume_a_job_paused_during_prepare(queue, monkeypatch):  # noqa: F811
    api, project, machine, scheduler = queue
    job = add(api, project, 'acc/team-owner-resume')
    call = machine.call

    async def pausing(method, **params):
        result = await call(method, **params)
        if method == 'gate_prepare':
            with write_db(api.app.state.db) as db:
                db.get(Setting, gates.BUDGET_KEY).value = {**gates.DEFAULT_BUDGET, 'enabled': False}
                db.commit()
        return result

    monkeypatch.setattr(api.app.state.supervisor.broker, 'call', pausing)
    await scheduler.tick()
    assert jobs(api)[job][0] == 'queued'
    assert machine.started == []
    monkeypatch.setattr(api.app.state.supervisor.broker, 'call', call)
    response = await api.put('/api/gates/budget', json={**gates.DEFAULT_BUDGET, 'enabled': True, 'confirm_branch_code': True})
    assert response.status_code == 200, response.text
    await scheduler.tick()
    assert machine.started == [(job, ['run-related', '{junit}'])]
    assert jobs(api)[job][0] == 'running'


@pytest.mark.parametrize('other_suite', [False, True])
async def test_budget_edits_preserve_gate_but_command_set_edits_invalidate_it(queue, monkeypatch, other_suite):  # noqa: F811
    api, project, machine, scheduler = queue
    job = add(api, project, 'acc/team-owner-edit')
    call = machine.call

    async def editing(method, **params):
        result = await call(method, **params)
        if method == 'gate_prepare':
            # The command for this job stays approved; another suite and the limits can change.
            with write_db(api.app.state.db) as db:
                row = db.get(Setting, gates.COMMANDS_KEY)
                config = row.value[str(project['id'])]
                if other_suite:
                    row.value = {str(project['id']): {**config, 'full': ['new-full', '{junit}']}}
                db.get(Setting, gates.BUDGET_KEY).value = {
                    **gates.DEFAULT_BUDGET, 'enabled': True, 'job_minutes': 15}
                db.commit()
        return result

    monkeypatch.setattr(api.app.state.supervisor.broker, 'call', editing)
    await scheduler.tick()
    if other_suite:
        assert machine.started == []
        assert jobs(api)[job][0] == 'refused'
    else:
        assert machine.started == [(job, ['run-related', '{junit}'])]
        assert machine.limits['seconds'] == 15 * 60
        assert jobs(api)[job][0] == 'running'


async def test_owner_pause_still_collects_success_from_an_already_running_gate(queue):  # noqa: F811
    api, project, machine, scheduler = queue
    job = add(api, project, 'acc/team-owner-running')
    await scheduler.tick()
    response = await api.put('/api/gates/budget', json={**gates.DEFAULT_BUDGET, 'enabled': False})
    assert response.status_code == 200, response.text
    machine.finished.add(job)
    machine.results[job] = {'exit': 0, 'counts': {'tests': 1, 'failed': 0}, 'failed': []}
    await scheduler.tick()
    assert len(machine.started) == 1
    assert jobs(api)[job][0] == 'passed'


@pytest.mark.parametrize('limit', ['gate_slots', 'full_suites_max'])
async def test_owner_reducing_capacity_during_prepare_stops_stale_plan_launches(queue, monkeypatch, limit):  # noqa: F811
    api, project, machine, scheduler = queue
    with write_db(api.app.state.db) as db:
        db.get(Setting, gates.BUDGET_KEY).value = {
            **gates.DEFAULT_BUDGET, 'enabled': True, 'gate_slots': 2, 'full_suites_max': 2}
        db.commit()
    first = add(api, project, 'acc/team-budget-first', suite='full')
    second = add(api, project, 'acc/team-budget-second', suite='full')
    call = machine.call

    async def reducing(method, **params):
        result = await call(method, **params)
        if method == 'gate_prepare':
            with write_db(api.app.state.db) as db:
                row = db.get(Setting, gates.BUDGET_KEY)
                row.value = {**row.value, limit: 1,
                             **({'full_suites_max': 1} if limit == 'gate_slots' else {})}
                db.commit()
        return result

    monkeypatch.setattr(api.app.state.supervisor.broker, 'call', reducing)
    await scheduler.tick()
    assert [identifier for identifier, _ in machine.started] == [first]
    assert jobs(api)[second][0] == 'queued'
    # The owner can enlarge the budget again without requeueing or losing the job.
    with write_db(api.app.state.db) as db:
        row = db.get(Setting, gates.BUDGET_KEY)
        row.value = {**row.value, 'gate_slots': 2, 'full_suites_max': 2}
        db.commit()
    monkeypatch.setattr(api.app.state.supervisor.broker, 'call', call)
    await scheduler.tick()
    assert [identifier for identifier, _ in machine.started] == [first, second]
