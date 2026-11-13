"""Person interrupt authority cannot be borrowed from G4 service delivery scopes."""
import httpx
import pytest
from test_session_relay_public_interrupts import mutate, native_calls, qualify_interrupt, start
from test_session_relay_public_writer import SID, attach, call, public_case  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401
from test_session_relay_service_delivery import configure, enqueue

from acc.server.models import RelayOperation, RelaySession
from acc.server.orchestrator import delivery
from acc.server.session_relay import delivery as service
from acc.server.session_relay.hub import Hub
from acc.session_relay import protocol as p


@pytest.fixture
async def joined_case(public_case, monkeypatch):  # noqa: F811
    return qualify_interrupt(configure(public_case, monkeypatch), monkeypatch)


async def test_person_writer_cannot_interrupt_accepted_service_work(joined_case):
    c = joined_case
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url='http://fixture') as client:
        view = await attach(client, 'person-over-service')
        params, identifier = await mutate(client, c, 'writer.take', {'view': 'chat', 'expectedRevision': 1,
            'reason': 'Person takes Chat before trusted delivery', 'attachmentId': view['attachmentId']}, 'take-chat')
        assert (await call(client, 'writer.take', {**params, 'operationId': identifier}))['result']['state'] == 'accepted'
        queued = enqueue(c)
        await delivery._deliver(c.service_supervisor, queued['id'])
        with c.db() as db:
            op = service.operation(db, queued['id'])
            active, original = op.id, dict(op.receipt)
            assert original['state'] == 'accepted' and original['actor']['kind'] == 'service'
        params, interrupt_id = await mutate(client, c, 'turn.interrupt', {'turnId': active,
            'expectedControlRevision': 2}, 'person-cannot-cancel-service')
        reply = await call(client, 'turn.interrupt', {**params, 'operationId': interrupt_id})
        assert reply['error']['data']['code'] == 'STALE_AUTHORITY', reply
        with c.db() as db:
            assert db.get(RelayOperation, active).receipt == original
            assert db.get(RelaySession, SID).active_operation_id == active
            assert db.get(RelayOperation, interrupt_id).receipt['state'] == 'failed'
        assert c.broker_state.adapter.active_turn == active
    assert native_calls(c) == [] and not c.launch.grants
    assert sum(method == 'turn/start' for method, _ in c.owners[0].calls) == 1
    assert not list(c.broker_state.staging.root.glob('*.payload'))


async def test_valid_service_scope_cannot_register_or_borrow_person_interrupt(joined_case):
    c = joined_case
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url='http://fixture') as client:
        await start(client, c)
        queued = enqueue(c)
        with c.db() as db:
            op = service.operation(db, queued['id'])
            resolver = service.DeliveryResolver(c.ctx, queued['id'], op.authority['delivery'])
            original_service = dict(op.receipt)
        params, interrupt_id = await mutate(client, c, 'turn.interrupt', {'turnId': c.active,
            'expectedControlRevision': 2}, 'real-person-interrupt')
        with c.db() as db:
            original_person = dict(db.get(RelayOperation, interrupt_id).receipt)
        # This is a real current scoped service resolver, not a fabricated actor.
        assert resolver()[1].kind is p.ActorKind.SERVICE
        with pytest.raises(p.RelayError) as refused:
            Hub(c.ctx).register(resolver, SID, str(c.launch.run_id), 'service-interrupt', 'turn.interrupt',
                                p.operation_digest('turn.interrupt', params))
        assert refused.value.code is p.ErrorCode.POLICY_REFUSED
        with pytest.raises(p.RelayError) as refused:
            await c.manager.interrupt(resolver, SID, {**params, 'operationId': interrupt_id})
        assert refused.value.code in (p.ErrorCode.NOT_FOUND_OR_DENIED, p.ErrorCode.POLICY_REFUSED)
        with c.db() as db:
            assert db.get(RelayOperation, interrupt_id).receipt == original_person
            assert service.operation(db, queued['id']).receipt == original_service
            assert db.get(RelaySession, SID).active_operation_id == c.active
        assert not native_calls(c) and not c.launch.grants
        # Refusal has not consumed or poisoned the person's immutable request.
        reply = await call(client, 'turn.interrupt', {**params, 'operationId': interrupt_id})
        assert reply['result']['state'] == 'accepted', reply
        assert await call(client, 'turn.interrupt', {**params, 'operationId': interrupt_id}) == reply
    assert len(native_calls(c)) == 1 and not c.launch.grants
    assert native_calls(c)[0][1]['turnId'] == c.broker_state.adapter.turns[c.active]
