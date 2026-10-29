"""A person's scope edits never wait for agents; stale helpers cannot integrate."""

import pytest
from helpers import pause_supervisor, write_db
from sqlalchemy import select
from test_orchestrator_assignments import assigned
from test_orchestrator_assignments import runtime as runtime
from test_team import request

from acc.server.models import AttentionItem, Session, utcnow
from acc.server.orchestrator import assignments, delivery, planning
from acc.server.supervisor_common import LaunchRefused


@pytest.fixture(autouse=True)
def enable_planning(monkeypatch):
    monkeypatch.setenv('ACC_ORCHESTRATOR_V2', '1')


@pytest.fixture
async def scope_helper(runtime, api, env, project):  # noqa: F811 (fixture)
    caller, assignment = await assigned(api, env, project)
    assert (await request(api, env, caller, 'plan_update', reason='Fixture scope', idea='CSV import',
        scope={'in': ['CSV import'], 'out': [], 'parked': []}, phases=[{'key': 'import', 'title': 'Import CSV'}]))['ok']
    assert (await request(api, env, caller, 'plan_ready', reason='Fixture ready'))['ok']
    picked = (await request(api, env, caller, 'select', assignment=assignment['id']))['result']
    started = await request(api, env, caller, 'start', assignment=assignment['id'], selection=picked['id'], name='CSV worker')
    assert started['ok'], started
    helper = started['result']
    await pause_supervisor(api)
    await delivery.stop_worker(api.app.state.supervisor)
    with write_db(api.app.state.db) as db:
        planning.draft_brief(db, caller['id'], '@api', 1, [{'text': 'Import CSV'}], [])
        row = db.get(assignments.Assignment, assignment['id'])
        row.requirement_ids, row.state = ['R-1'], 'reported'
        db.get(assignments.Attempt, helper['id']).state = 'reported'
        version = planning.plan_for(db, caller['id']).version
        db.commit()
    try:
        yield caller, assignment, helper, version
    finally:
        await delivery.stop_worker(api.app.state.supervisor)


@pytest.mark.parametrize('enabled', [False, True])
async def test_person_change_is_accepted_and_failed_delivery_has_stop_notice(scope_helper, api, monkeypatch, enabled):
    caller, assignment, helper, version = scope_helper
    monkeypatch.setenv('ACC_ORCHESTRATOR_V2_DELIVERY', '1' if enabled else '0')
    response = await api.patch(f"/api/teams/{caller['id']}/plan", json={'version': version,
        'requirements': [{'id': 'R-1', 'text': 'Import CSV without external uploads'}]})
    assert response.status_code == 200, response.text
    with write_db(api.app.state.db) as db:
        notice = db.scalar(select(planning.PlanChangeNotice).where(planning.PlanChangeNotice.session_id == helper['id']))
        assert notice is not None
        if enabled:
            message = db.get(delivery.MessageDelivery, notice.delivery_id)
            delivery.transition(db, message, 'failed', error='submission_unconfirmed')
        db.commit()
    with api.app.state.db() as db:
        item = db.scalar(select(AttentionItem).where(AttentionItem.kind == 'scope_change', AttentionItem.session_id == helper['id'], AttentionItem.resolved_at.is_(None)))
        assert item is not None and helper['name'] in item.title
        assert 'Stop' in item.detail
        assert db.get(assignments.Assignment, assignment['id']).state == 'needs_revalidation'
    for action in ('merge', 'finish'):
        with pytest.raises(LaunchRefused, match='scope version'):
            await api.app.state.supervisor.team_action(caller['id'], {'action': action, 'helper': helper['id']})


async def test_acknowledgement_covers_only_the_delivered_scope(scope_helper, api, monkeypatch):
    from acc.server.orchestrator import scope_notices
    caller, _, helper, version = scope_helper
    monkeypatch.setenv('ACC_ORCHESTRATOR_V2_DELIVERY', '0')
    for number in (1, 2):
        response = await api.patch(f"/api/teams/{caller['id']}/plan", json={'version': version + number - 1,
            'requirements': [{'id': 'R-1', 'text': f'Accepted CSV scope {number}'}]})
        assert response.status_code == 200, response.text
        if number == 1:
            with write_db(api.app.state.db) as db:
                notice = db.scalar(select(planning.PlanChangeNotice).where(planning.PlanChangeNotice.session_id == helper['id']))
                delivery.transition(db, db.get(delivery.MessageDelivery, notice.delivery_id), 'acknowledged', ack='hook:UserPromptSubmit')
                scope_notices.require_integration(db, db.get(Session, helper['id']))
                db.commit()
    with api.app.state.db() as db:
        with pytest.raises(LaunchRefused, match='scope version'):
            scope_notices.require_integration(db, db.get(Session, helper['id']))


async def test_restart_receipt_requires_a_verified_run_and_the_rendered_version(scope_helper, api, monkeypatch):
    from acc.server.models import Event, SessionRun
    from acc.server.orchestrator import scope_notices
    caller, _, helper, version = scope_helper
    monkeypatch.setenv('ACC_ORCHESTRATOR_V2_DELIVERY', '0')
    assert (await api.patch(f"/api/teams/{caller['id']}/plan", json={'version': version,
        'requirements': [{'id': 'R-1', 'text': 'New accepted boundary'}]})).status_code == 200
    with write_db(api.app.state.db) as db:
        member = db.get(Session, helper['id'])
        run = SessionRun(session_id=member.id, reason='manual_resume')
        db.add(run)
        db.flush()
        receipt = Event(session_id=member.id, source='supervisor', kind='run_manual_resume',
            payload={'scope_run_id': run.id, 'scope_version': version})
        db.add(receipt)
        db.flush()
        with pytest.raises(LaunchRefused, match='scope version'):
            scope_notices.require_integration(db, member)
        run.verified_at = utcnow()
        with pytest.raises(LaunchRefused, match='scope version'):
            scope_notices.require_integration(db, member)
        receipt.payload = {**receipt.payload, 'scope_version': version + 1}
        db.flush()
        scope_notices.require_integration(db, member)
        # A later scope edit cannot be acknowledged by this earlier launch.
        planning.person_update(db, caller['id'], '@api', {'version': version + 1,
            'requirements': [{'id': 'R-1', 'text': 'A still later boundary'}]})
        with pytest.raises(LaunchRefused, match='scope version'):
            scope_notices.require_integration(db, member)
        db.rollback()


async def test_real_restart_delivers_current_scope_before_it_unblocks_integration(scope_helper, api, env, monkeypatch):
    from pathlib import Path

    from helpers import wait_for

    from acc.server.orchestrator import scope_notices
    caller, _, helper, version = scope_helper
    monkeypatch.setenv('ACC_ORCHESTRATOR_V2_DELIVERY', '0')
    changed = await api.patch(f"/api/teams/{caller['id']}/plan", json={'version': version,
        'requirements': [{'id': 'R-1', 'text': 'The new accepted CSV boundary'}]})
    assert changed.status_code == 200, changed.text
    sup = api.app.state.supervisor
    await sup.stop(helper['id'], '@api')
    with sup.db() as db:
        with pytest.raises(LaunchRefused, match='scope version'):
            scope_notices.require_integration(db, db.get(Session, helper['id']))
    await sup.resume(helper['id'], '@api')
    async def restarted():
        await sup.reconcile()
        with sup.db() as db:
            return not scope_notices.pending(db, db.get(Session, helper['id']))
    await wait_for(restarted)
    text = (Path(env['run_dir']) / helper['id'] / 'append-prompt.md').read_text()
    assert 'The new accepted CSV boundary' in text
    with sup.db() as db:
        scope_notices.require_integration(db, db.get(Session, helper['id']))
        assert not list(db.scalars(select(AttentionItem).where(AttentionItem.session_id == helper['id'],
            AttentionItem.kind == 'scope_change', AttentionItem.resolved_at.is_(None))))


async def test_notice_coalesced_during_safety_probe_delivers_the_new_version(scope_helper, api, env, monkeypatch):
    from pathlib import Path

    caller, _, helper, version = scope_helper
    sup = api.app.state.supervisor
    monkeypatch.setenv('ACC_ORCHESTRATOR_V2_DELIVERY', '0')
    assert (await api.patch(f"/api/teams/{caller['id']}/plan", json={'version': version,
        'requirements': [{'id': 'R-1', 'text': 'First revised boundary'}]})).status_code == 200
    await sup.reconcile()
    with sup.db() as db:
        message_id = db.scalar(select(planning.PlanChangeNotice.delivery_id).where(
            planning.PlanChangeNotice.session_id == helper['id']))
    original = delivery.safety
    changed_once = False
    async def coalesce(*args, **kwargs):
        nonlocal changed_once
        if changed_once:
            return await original(*args, **kwargs)
        changed_once = True
        changed = await api.patch(f"/api/teams/{caller['id']}/plan", json={'version': version + 1,
            'requirements': [{'id': 'R-1', 'text': 'Final revised boundary'}]})
        assert changed.status_code == 200, changed.text
        return await original(*args, **kwargs)
    monkeypatch.setattr(delivery, 'safety', coalesce)
    monkeypatch.setattr(delivery, 'wake', lambda supervisor: None)
    monkeypatch.setenv('ACC_ORCHESTRATOR_V2_DELIVERY', '1')
    await delivery.deliver(sup, message_id)
    with sup.db() as db:
        assert db.get(delivery.MessageDelivery, message_id).state == 'acknowledged'
    actual = (Path(env['run_dir']) / helper['id'] / 'fake.received').read_text()
    assert f'Plan change notice version {version + 2}:' in actual


async def test_lead_proposals_never_reach_helpers_as_accepted_scope(scope_helper, api):
    """Final security audit 2: a lead's unanswered proposal stays out of the scope helpers receive as accepted."""
    from acc.server.orchestrator.scope_notices import launch_scope
    caller, assignment, helper, _ = scope_helper
    lead_text = 'Uploads may go to any external host'
    with write_db(api.app.state.db) as db:
        planning.draft_brief(db, caller['id'], f"session:{caller['id']}", 1, [{'text': 'Lead-proposed requirement'}],
                             [{'text': lead_text}])
        db.commit()
    with api.app.state.db() as db:
        _, scope = launch_scope(db, db.get(Session, helper['id']))
        context = assignments.planning_context(db, db.get(assignments.Assignment, assignment['id']))
    assert 'Import CSV' in scope and lead_text not in scope and 'Lead-proposed requirement' not in scope
    assert all(row['state'] == 'active' for row in context['constraints'] + context['requirements'])
    assert lead_text not in str(context)
