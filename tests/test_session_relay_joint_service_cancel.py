# ruff: noqa: F811
"""Old service scopes cannot cancel queued work behind an actual replacement turn."""
import asyncio
from datetime import timedelta

import pytest
from test_session_relay_runtime import runtime_case  # noqa: F401
from test_session_relay_service_delivery import enqueue, service_case, team_case  # noqa: F401

from acc.server.db import write_intent
from acc.server.models import RelaySession, SessionRun, utcnow
from acc.server.orchestrator import delivery
from acc.server.orchestrator.assignments import Assignment, Attempt
from acc.server.session_relay import delivery as service
from acc.session_relay import protocol as p

SID = 'abc123def456'


@pytest.mark.parametrize('change', ['none', 'expiry', 'credential', 'budget', 'login'])
async def test_old_api_scope_cannot_cancel_after_native_turn_replacement(service_case, monkeypatch, change):
    await replacement_cancel(service_case, monkeypatch, change)


@pytest.mark.parametrize('change', ['none', 'assignment', 'attempt', 'source_run'])
async def test_old_team_scope_cannot_cancel_after_native_turn_replacement(team_case, monkeypatch, change):
    await replacement_cancel(team_case, monkeypatch, change, actor='session:fixture-team-lead')


async def replacement_cancel(c, monkeypatch, change, actor='@api'):
    first, second = enqueue(c, 'Original active service work', actor=actor), enqueue(c, 'Replacement service work', actor=actor)
    await delivery._deliver(c.service_supervisor, first['id'])
    await delivery._deliver(c.service_supervisor, second['id'])
    with c.db() as db:
        one = service.operation(db, first['id'])
        old_resolve = service.DeliveryResolver(c.ctx, first['id'], one.authority['delivery'])
        original_id = one.id
        replacement_id = service.operation(db, second['id']).id
    # Genuine fixture native completion drives the normal main queue drain.
    await c.service_state.events.put({'method': 'turn/completed', 'params': {
        'threadId': 'fixture-native-id', 'turn': {'id': c.service_state.adapter.turns[original_id], 'status': 'completed'}}})
    for _ in range(300):
        with c.db() as db:
            replacement = service.operation(db, second['id'])
            receipt = dict(replacement.receipt)
        if receipt['state'] == 'accepted':
            break
        await asyncio.sleep(.01)
    assert receipt['state'] == 'accepted'
    assert c.service_state.adapter.active_turn == replacement_id
    third = enqueue(c, 'Queued behind replacement service work', actor=actor)
    await delivery._deliver(c.service_supervisor, third['id'])
    with c.db() as db:
        target = service.operation(db, third['id'])
        params = {'operationId': target.id, 'expectedRevision': target.receipt['revision']}
        queued_receipt = dict(target.receipt)
        assert queued_receipt['state'] == 'queued'
    assert old_resolve()[1].kind is p.ActorKind.SERVICE
    grants = set(c.launch.grants)
    staged = {path.name: path.read_bytes() for path in c.service_state.staging.root.glob('*.payload')}
    assert len(grants) == len(staged) == 1

    @write_intent
    def revoke():
        with c.db() as db:
            if change == 'expiry':
                db.get(delivery.MessageDelivery, first['id']).expires_at = utcnow() - timedelta(seconds=1)
            elif change == 'credential':
                c.ctx.token = 'synthetic-rotated-token'
            elif change == 'login':
                c.state['revoked'] = True
            elif change == 'assignment':
                db.get(Assignment, 'a' * 32).state = 'cancelled'
            elif change == 'attempt':
                db.add(Attempt(id='c' * 32, assignment_id='a' * 32, session_id=SID, sequence=2, state='running'))
            elif change == 'source_run':
                db.add(SessionRun(session_id='fixture-team-lead', reason='replacement', verified_at=utcnow()))
            db.commit()

    revoke()
    if change == 'budget':
        monkeypatch.setattr(service.budgets, 'paused', lambda *_a, **_kw: ['fixture exhausted budget'])
    # A stale scope must refuse before deleting even another operation's unsent
    # stage. The accepted replacement turn remains independently untouched.
    from fastapi import HTTPException
    with pytest.raises((p.RelayError, HTTPException)):
        await c.manager.cancel(old_resolve, SID, params)
    with c.db() as db:
        assert service.operation(db, third['id']).receipt == queued_receipt
        assert service.operation(db, second['id']).receipt == receipt
        assert db.get(RelaySession, SID).active_operation_id == replacement_id
    assert set(c.launch.grants) == grants
    assert {path.name: path.read_bytes() for path in c.service_state.staging.root.glob('*.payload')} == staged
    assert c.service_state.adapter.active_turn == replacement_id
    assert sum(method == 'turn/start' for method, _ in c.owners[0].calls) == 2
    assert not any(method == 'turn/interrupt' for method, _ in c.owners[0].calls)
