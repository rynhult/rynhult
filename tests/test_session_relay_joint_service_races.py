# ruff: noqa: F811
"""Joined G4 delivery and G237 final-admission fences; synthetic native owner."""
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

SID = 'abc123def456'


@pytest.mark.parametrize('boundary', ['stage', 'probe'])
@pytest.mark.parametrize('change', ['credential', 'deadline', 'budget', 'holder', 'revision'])
async def test_service_authority_changes_across_await_refuse_native_start(service_case, monkeypatch, boundary, change):
    await refused_after_await(service_case, monkeypatch, boundary, change)


@pytest.mark.parametrize('boundary', ['stage', 'probe'])
@pytest.mark.parametrize('change', ['assignment', 'attempt', 'source_run'])
async def test_team_authority_changes_across_await_refuse_native_start(team_case, monkeypatch, boundary, change):
    await refused_after_await(team_case, monkeypatch, boundary, change, actor='session:fixture-team-lead')


async def refused_after_await(c, monkeypatch, boundary, change, actor='@api'):
    queued = enqueue(c, actor=actor)
    reached, release = asyncio.Event(), asyncio.Event()
    original_call, original_probe = c.launch.peer.call, c.service_state.adapter.probe

    async def paused_call(method, params):
        result = await original_call(method, params)
        if method == 'stage' and boundary == 'stage':
            reached.set()
            await release.wait()
        return result

    async def paused_probe(binding):
        result = await original_probe(binding)
        if boundary == 'probe':
            reached.set()
            await release.wait()
        return result

    c.launch.peer.call, c.service_state.adapter.probe = paused_call, paused_probe
    pending = asyncio.create_task(delivery._deliver(c.service_supervisor, queued['id']))
    try:
        await asyncio.wait_for(reached.wait(), 5)

        @write_intent
        def revoke():
            with c.db() as db:
                row = db.get(RelaySession, SID)
                if change == 'credential':
                    c.ctx.token = 'synthetic-rotated-token'
                elif change == 'deadline':
                    db.get(delivery.MessageDelivery, queued['id']).expires_at = utcnow() - timedelta(seconds=1)
                elif change == 'holder':
                    row.controller = {**row.controller, 'principal': 'sam'}
                elif change == 'revision':
                    row.control_revision += 1
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
        release.set()
        await asyncio.wait_for(pending, 5)
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
    with c.db() as db:
        op = service.operation(db, queued['id'])
        assert op.receipt['state'] == 'failed', op.receipt
        assert db.get(delivery.MessageDelivery, queued['id']).state == 'failed'
        assert db.get(RelaySession, SID).active_operation_id is None
    assert not any(method in ('turn/start', 'turn/interrupt') for method, _ in c.owners[0].calls)
    assert not c.service_state.adapter.attempted
    assert not c.launch.grants
    assert not list(c.service_state.staging.root.glob('*.payload'))


async def test_conclusive_probe_refusal_does_not_block_fresh_service_work(service_case, monkeypatch):
    c = service_case
    await refused_after_await(c, monkeypatch, 'probe', 'credential')
    c.ctx.token = 'synthetic-delivery-token'
    fresh = enqueue(c, 'Fresh authority after rejected old claim')
    await delivery._deliver(c.service_supervisor, fresh['id'])
    with c.db() as db:
        op = service.operation(db, fresh['id'])
        assert op.receipt['state'] == 'accepted'
        assert db.get(RelaySession, SID).active_operation_id == op.id
    assert sum(method == 'turn/start' for method, _ in c.owners[0].calls) == 1
    assert not c.launch.grants
