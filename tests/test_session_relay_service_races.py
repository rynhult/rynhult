"""Actual public HTTP take versus scoped delivery on typed enrolled-node transport."""
import asyncio

import httpx
import pytest
from fastapi import FastAPI, Request
from sqlalchemy import select
from test_session_relay_node_runtime import node_runtime  # noqa: F401
from test_session_relay_public_writer import attach, call, register
from test_session_relay_runtime import runtime_case  # noqa: F401
from test_session_relay_service_delivery import SID, configure, enqueue
from test_session_relay_writer_routes import retire

from acc.server.api.session_relay import router
from acc.server.models import AuditLog, RelayOperation, RelaySession
from acc.server.orchestrator import delivery
from acc.server.session_relay import delivery as service
from acc.server.supervisor import LaunchRefused


@pytest.fixture
async def node_service_public_case(node_runtime, monkeypatch):  # noqa: F811
    c = node_runtime
    await c.launch.launch(SID)
    configure(c, monkeypatch)
    def principal(request: Request):
        request.state.cookie_authenticated = True
        request.state.session_hash = 'synthetic-login'
        return c.resolve()[0]
    c.ctx.principal = principal
    c.ctx.relay_principal = lambda _db, request: principal(request)
    c.app = FastAPI()
    c.app.include_router(router(c.ctx))
    c.service_state.writers.retire = retire
    return c


async def test_registered_service_turn_wins_before_public_take_and_keeps_actor(node_service_public_case):
    c = node_service_public_case
    queued = enqueue(c)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url='http://fixture') as client:
        view = await attach(client, 'delivery-race')
        params = {'expectedRunId': str(c.launch.run_id), 'attachmentId': view['attachmentId'],
                  'expectedRevision': 1, 'view': 'terminal', 'reason': 'Concurrent person take'}
        operation = (await register(client, c, params, 'race-take'))['result']['operationId']
        await delivery._deliver(c.service_supervisor, queued['id'])
        refused = await call(client, 'writer.take', {**params, 'operationId': operation})
        assert refused['error']['data']['code'] == 'BUSY'
    with c.db() as db:
        assert service.operation(db, queued['id']).receipt['actor']['kind'] == 'service'
        assert service.operation(db, queued['id']).receipt['state'] == 'accepted'
        assert db.get(RelayOperation, operation).receipt['state'] == 'failed'
        assert db.get(RelaySession, SID).control_revision == 1
        audit = db.scalars(select(AuditLog).where(AuditLog.action == 'relay.delivery.settled')).one()
        assert audit.actor == '@api' and audit.detail['channel'] == 'api'
        assert audit.detail['previous']['kind'] == 'person' and audit.detail['previous']['channel'] == 'chat'
    assert c.service_state.writers.gate.epoch == 1 and len(c.wire.opens) == 1
    assert sum(method == 'turn/start' for method, _ in c.owners[0].calls) == 1


async def test_public_take_boundary_refuses_new_service_without_partial_queue(node_service_public_case):
    c = node_service_public_case
    reached, release = asyncio.Event(), asyncio.Event()
    async def blocked(lease):
        reached.set()
        await release.wait()
        return await retire(lease)
    c.service_state.writers.retire = blocked
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url='http://fixture') as client:
        view = await attach(client, 'take-wins')
        params = {'expectedRunId': str(c.launch.run_id), 'attachmentId': view['attachmentId'],
                  'expectedRevision': 1, 'view': 'terminal', 'reason': 'Take before delivery'}
        operation = (await register(client, c, params, 'first-take'))['result']['operationId']
        pending = asyncio.create_task(call(client, 'writer.take', {**params, 'operationId': operation}))
        try:
            await asyncio.wait_for(reached.wait(), 3)
            with pytest.raises(LaunchRefused):
                enqueue(c)
        finally:
            release.set()
        assert (await asyncio.wait_for(pending, 3))['result']['state'] == 'accepted'
        with pytest.raises(LaunchRefused):
            enqueue(c)
    with c.db() as db:
        assert not list(db.scalars(select(delivery.MessageDelivery)))
        assert len(list(db.scalars(select(RelayOperation)))) == 1
        assert db.get(RelaySession, SID).control_revision == 2
        audit = db.scalars(select(AuditLog).where(AuditLog.action == 'relay.writer.take')).one()
        assert audit.detail['previous']['principal'] == 'alex' and audit.detail['previous']['channel'] == 'chat'
    assert c.service_state.writers.gate.epoch == 2 and len(c.wire.opens) == 1
    assert not any(method == 'turn/start' for method, _ in c.owners[0].calls)
