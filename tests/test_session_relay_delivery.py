"""Relay admission refuses legacy text before broker IO, including retained queues."""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from relay_helpers import setup

from acc.server.db import write_intent
from acc.server.models import Session, utcnow
from acc.server.orchestrator import delivery
from acc.server.supervisor import LaunchRefused


@pytest.fixture
def case(tmp_path):
    case = setup(tmp_path)
    case.ctx._get = lambda db, identifier: db.get(Session, identifier)
    yield case
    case.engine.dispose()


def test_team_and_schedule_enqueue_refuse_relay_before_redaction_or_typing(case):
    with case.db() as db:
        session = db.get(Session, 'relay')
        with pytest.raises(LaunchRefused, match='qualified relay delivery'):
            delivery.enqueue_in_transaction(db, case.ctx, session, 'alex', 'Artificial task')


async def test_retained_queue_fails_without_broker_effect_or_global_delivery_stall(case):
    @write_intent
    def seed():
        with case.db() as db:
            row = delivery.MessageDelivery(session_id='relay', actor='alex', preview='old task', body='old task',
                 text_sha256='0' * 64, bytes=8, expires_at=utcnow() + timedelta(minutes=5))
            db.add(row)
            db.commit()
            return row.id
    identifier = seed()
    # No broker attribute: even auth refresh, peer lookup, screen inspection or
    # opening a stream would fail this test. Relay refusal returns immediately.
    await delivery._deliver(case.ctx, identifier)
    with case.db() as db:
        row = db.get(delivery.MessageDelivery, identifier)
        assert row.state == 'failed' and row.error == 'relay_unavailable'
        assert row.body == '' and row.attempts == 0
    assert not await delivery.safety(case.ctx, 'relay')
    # Another session's dispatch remains schedulable after the refusal.
    import asyncio
    observed = []
    async def unrelated():
        observed.append('other session progressed')
    await asyncio.wait_for(asyncio.gather(delivery._deliver(case.ctx, identifier), unrelated()), 1)
    assert observed == ['other session progressed']


async def test_all_legacy_start_entry_points_refuse_before_provider_io(case):
    from acc.server.reconcile import ReconcileMixin
    with case.db() as db, pytest.raises(LaunchRefused, match='qualified relay recovery'):
        await ReconcileMixin._start(SimpleNamespace(), db, db.get(Session, 'relay'), 'resume')


@pytest.mark.parametrize('state', ['typing', 'acknowledged', 'failed', 'submitted'])
async def test_relay_fence_preserves_nonqueued_delivery(case, state):
    @write_intent
    def seed():
        with case.db() as db:
            row = delivery.MessageDelivery(session_id='relay', actor='alex', preview='retained', body='retained',
                 text_sha256='0' * 64, bytes=8, state=state, expires_at=utcnow() + timedelta(minutes=5))
            db.add(row)
            db.commit()
            return row.id
    identifier = seed()
    await delivery._deliver(case.ctx, identifier)
    with case.db() as db:
        row = db.get(delivery.MessageDelivery, identifier)
        assert row.state == state and row.body == 'retained'


async def test_enrollment_during_await_is_fenced_under_final_claim_lock(case, monkeypatch):
    from acc.server import claude_auth
    from acc.server.models import RelayRun, RelaySession
    from acc.server.session_relay import store
    @write_intent
    def seed():
        with case.db() as db:
            db.delete(db.get(RelayRun, 1))
            db.delete(db.get(RelaySession, 'relay'))
            row = delivery.MessageDelivery(session_id='relay', actor='alex', preview='queued', body='queued',
                 text_sha256='0' * 64, bytes=6, expires_at=utcnow() + timedelta(minutes=5))
            db.add(row)
            db.commit()
            return row.id
    identifier = seed()
    async def refresh(*_):
        pass
    async def safe(*_):
        return True
    async def cancel(*_):
        pass
    async def enroll(*_):
        with case.db() as db:
            store.enroll(db, 'relay', 1, 'new-binding', identity={'fixture': True}, frozen_policy={}, policy_revision=0)
            db.commit()
    monkeypatch.setattr(claude_auth, 'refresh', refresh)
    monkeypatch.setattr(claude_auth, 'require', lambda *_: None)
    monkeypatch.setattr(delivery, 'safety', safe)
    monkeypatch.setattr(delivery, 'ingest', enroll)
    case.ctx.cancel_automation = cancel
    # No broker exists: a failure to fence under the final lock would attempt IO.
    await delivery._deliver(case.ctx, identifier)
    with case.db() as db:
        row = db.get(delivery.MessageDelivery, identifier)
        assert row.state == 'failed' and row.error == 'relay_unavailable'
        assert row.attempts == 0
