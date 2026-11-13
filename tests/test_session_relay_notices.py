"""Immutable coalesced system intents, fresh grants, no notice replay."""
import pytest
from sqlalchemy import select
from test_session_relay_runtime import runtime_case  # noqa: F401
from test_session_relay_service_delivery import SID, configure

from acc.server.db import write_intent
from acc.server.models import RelaySession, Session, SessionRun, Team
from acc.server.orchestrator import delivery
from acc.server.session_relay import delivery as service


@pytest.fixture
async def notice_case(runtime_case, monkeypatch):  # noqa: F811
    c = runtime_case
    monkeypatch.setattr(service, 'DELIVERY_QUALIFIED', True)
    @write_intent
    def seed():
        with c.db() as db:
            db.delete(db.get(RelaySession, SID))
            db.delete(db.get(SessionRun, c.launch.run_id))
            db.flush()
            db.get(Session, SID).team_id = SID
            db.add(Team(id=SID, limits={'at_once': 2, 'total': 3, 'depth': 2}))
            db.commit()
        c.launch = c.manager.new_launch(c.resolve)
        with c.db() as db:
            c.launch.reserve(db, db.get(Session, SID))
            db.commit()
    seed()
    await c.launch.launch(SID)
    return configure(c, monkeypatch)


@write_intent
def nudge(c, kind):
    with c.db() as db:
        row = delivery.nudge_lead(db, SID, kind)
        identifier = row.id
        db.commit()
        return identifier


async def assert_notice_refused(c, identifier):
    """Excluded notices expose a fixed reason and never acquire native authority."""
    await delivery._deliver(c.service_supervisor, identifier)
    with c.db() as db:
        row = db.get(delivery.MessageDelivery, identifier)
        visible = delivery.public(row)
        assert row.state == 'failed', visible
        assert visible['error'] == 'relay_not_supported_in_release', visible
        assert 'not supported in this release' in visible['notice'], visible
        assert service.operation(db, identifier) is None
    assert not c.launch.grants
    assert not list(c.service_state.staging.root.glob('*.payload'))
    assert not any(method == 'turn/start' for method, _ in c.owners[0].calls)


async def test_coalescing_supersedes_intent_identity_and_keeps_schedule_actor(notice_case):
    c = notice_case
    first = nudge(c, 'plan')
    second = nudge(c, 'answer')
    assert first != second
    with c.db() as db:
        assert db.get(delivery.MessageDelivery, first).state == 'expired'
        assert service.notice_scope(db, db.get(delivery.MessageDelivery, second))['family'] == 'lead'
    await assert_notice_refused(c, second)


async def test_staged_notice_supersession_preserves_digest_and_reclaims_only_failed_stage(notice_case):
    c = notice_case
    first = nudge(c, 'plan')
    await assert_notice_refused(c, first)
    second = nudge(c, 'answer')
    await assert_notice_refused(c, second)
    # R1 never stages either excluded notice, including a superseding one.
    assert first != second


async def test_notice_intent_waits_without_runtime_and_never_starts_one(notice_case):
    c = notice_case
    identifier = nudge(c, 'plan')
    run = c.manager.runs.pop(SID)
    try:
        await assert_notice_refused(c, identifier)
    finally:
        c.manager.runs[SID] = run
    await assert_notice_refused(c, identifier)


async def test_intent_is_not_derived_from_supervisor_label_or_body(notice_case):
    c = notice_case
    @write_intent
    def untrusted():
        from datetime import timedelta

        from acc.server.models import utcnow
        with c.db() as db:
            row = delivery.MessageDelivery(session_id=SID, actor='supervisor', body=delivery.NUDGE_ORIGIN + 'edited the plan',
                preview='unproven notice', text_sha256='0'*64, bytes=40, expires_at=utcnow()+timedelta(minutes=5))
            db.add(row)
            db.commit()
            return row.id
    identifier = untrusted()
    await delivery._deliver(c.service_supervisor, identifier)
    with c.db() as db:
        assert db.get(delivery.MessageDelivery, identifier).state == 'failed'
        assert service.operation(db, identifier) is None
    await assert_notice_refused(c, identifier)


async def test_submitting_or_unknown_notice_is_not_mutated_or_replayed(notice_case):
    c = notice_case
    identifier = nudge(c, 'plan')
    await assert_notice_refused(c, identifier)
    with c.db() as db:
        original = delivery.public(db.get(delivery.MessageDelivery, identifier))
    await assert_notice_refused(c, identifier)
    with c.db() as db:
        assert delivery.public(db.get(delivery.MessageDelivery, identifier)) == original


async def test_run_mode_notice_stays_pending_while_stopped_without_native_or_registration(notice_case):
    c = notice_case
    @write_intent
    def notices():
        with c.db() as db:
            db.get(Session, SID).desired_state = 'stopped'
            first = delivery.run_mode_notice(db, c.service_supervisor, SID, 'prepare', 1)
            first_id = first.id
            second = delivery.run_mode_notice(db, c.service_supervisor, SID, 'build', 2)
            identifier = second.id
            db.commit()
            return first_id, identifier
    first, second = notices()
    with c.db() as db:
        assert db.get(delivery.MessageDelivery, first).state == 'expired'
        assert db.get(delivery.MessageDelivery, second).state == 'queued'
        assert service.operation(db, second) is None
        assert service.notice_scope(db, db.get(delivery.MessageDelivery, second))['family'] == 'run_mode'
        assert db.get(Session, SID).desired_state == 'stopped'
    await assert_notice_refused(c, second)


async def test_real_project_brief_notifier_creates_schedule_intent(notice_case):
    from acc.server.orchestrator import project_briefs
    c = notice_case
    @write_intent
    def notify():
        with c.db() as db:
            project_briefs.notify_accept(db, c.service_supervisor, None, 2)
            row = db.scalars(select(delivery.MessageDelivery).where(delivery.MessageDelivery.session_id == SID)).one()
            identifier = row.id
            db.commit()
            return identifier
    identifier = notify()
    with c.db() as db:
        assert service.notice_scope(db, db.get(delivery.MessageDelivery, identifier))['family'] == 'project_brief'
    await assert_notice_refused(c, identifier)


async def test_parent_replacement_preserves_original_batch_window_and_expiry(notice_case):
    c = notice_case
    @write_intent
    def replace():
        with c.db() as db:
            first = delivery.nudge_parent(db, team_id=SID, parent_id=SID, name='one', status='reported')
            first_id, after, expiry = first.id, first.deliver_after, first.expires_at
            second = delivery.nudge_parent(db, team_id=SID, parent_id=SID, name='two', status='reported')
            assert second.id != first_id
            assert second.deliver_after == after and second.expires_at == expiry
            assert 'helper one' in second.body and 'helper two' in second.body
            db.commit()
            return second.id
    await assert_notice_refused(c, replace())


@pytest.mark.parametrize('change', ['team-grant', 'assignment', 'sealed-text'])
async def test_unbound_notice_cannot_acquire_a_grant_after_its_intent_changes(notice_case, change):
    """Deferred registration must not turn an old notice into fresh authority."""
    c = notice_case
    identifier = nudge(c, 'plan')
    @write_intent
    def invalidate():
        with c.db() as db:
            assert service.operation(db, identifier) is None
            if change == 'team-grant':
                team = db.get(Team, SID)
                team.limits = {**team.limits, 'total': 4}
            elif change == 'assignment':
                from acc.server.orchestrator.assignments import Assignment, Attempt
                db.add(Assignment(id='d'*32, team_id=SID, created_by=SID, task_type='build',
                    goal='Replacement assignment after notice', acceptance=[], requirement_ids=[], need={}, state='running'))
                db.flush()
                db.add(Attempt(id='e'*32, assignment_id='d'*32, session_id=SID, sequence=1, state='running'))
            else:
                import hashlib
                message = db.get(delivery.MessageDelivery, identifier)
                message.body = 'Altered after the server sealed the notice intent'
                message.text_sha256 = hashlib.sha256(message.body.encode()).hexdigest()
            db.commit()
    invalidate()
    await delivery._deliver(c.service_supervisor, identifier)
    with c.db() as db:
        assert db.get(delivery.MessageDelivery, identifier).state == 'failed'
        assert service.operation(db, identifier) is None
    await assert_notice_refused(c, identifier)


async def test_supersession_committed_before_binding_lock_cannot_leave_a_live_operation(notice_case, monkeypatch):
    """Model READ COMMITTED visibility after a waited row lock, without live PG."""
    from sqlalchemy import update

    from acc.server.session_relay import store
    c = notice_case
    identifier = nudge(c, 'plan')
    original = store.lock
    superseded = []

    def after_other_transaction(db, session_id):
        if not superseded:
            superseded.append(True)
            # The worker already has its pre-lock ORM snapshot. A coalescer has
            # committed a terminal row while it waited for the session lock.
            db.execute(update(delivery.MessageDelivery).where(delivery.MessageDelivery.id == identifier)
                .values(state='expired', body='', error='notice_superseded'),
                execution_options={'synchronize_session': False})
            db.commit()  # keep the pre-lock ORM object, as READ COMMITTED does
        return original(db, session_id)

    monkeypatch.setattr(store, 'lock', after_other_transaction)
    await delivery._deliver(c.service_supervisor, identifier)
    assert superseded
    with c.db() as db:
        assert db.get(delivery.MessageDelivery, identifier).state == 'expired'
        assert service.operation(db, identifier) is None
    assert not any(method == 'turn/start' for method, _ in c.owners[0].calls)

    monkeypatch.setattr(store, 'lock', original)
    await assert_notice_refused(c, nudge(c, 'answer'))
