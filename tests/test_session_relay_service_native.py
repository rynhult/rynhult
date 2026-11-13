"""Actual pinned Codex + OS broker, main grants over production Relay/NodeLink.

Artificial API/person/node enrollment and offline model; no TLS/partition claim.
"""
import asyncio

import pytest
from test_session_relay_node_runtime import node_runtime  # noqa: F401
from test_session_relay_process_restart import process_node  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401
from test_session_relay_service_delivery import SID, enqueue

from acc.server.db import write_intent
from acc.server.models import RelaySession, Session
from acc.server.orchestrator import delivery
from acc.server.session_relay import delivery as service
from acc.server.supervisor import LaunchRefused
from acc.session_relay import protocol as p

pytestmark = pytest.mark.relay_native


@pytest.mark.parametrize('loss', [False, True], ids=['accepted', 'lost-admission-result'])
async def test_native_service_delivery_retains_actor_and_one_writer_over_typed_node(process_node, monkeypatch, record_property, loss):  # noqa: F811
    c = process_node
    await c.launch.launch(SID)
    c.ctx.relay_runtime, c.ctx.token = c.manager, 'synthetic-native-service-token'
    supervisor = c.ctx.supervisor
    supervisor.db, supervisor.relay_manager = c.db, c.manager
    supervisor._get = lambda db, identifier: db.get(Session, identifier)
    c.service_supervisor = supervisor
    monkeypatch.setattr(service, 'DELIVERY_QUALIFIED', True)
    monkeypatch.setattr('acc.server.session_relay.writers.PUBLIC_WRITER_QUALIFIED', True)
    monkeypatch.setattr(delivery, 'enabled', lambda: True)
    @write_intent
    def qualify():
        with c.db() as db:
            db.get(RelaySession, SID).capabilities = {'text': 'qualified', 'terminal.input': 'qualified'}
            db.commit()
    qualify()
    await c.inspect('qualify')
    if loss:
        await c.inspect('uncertain')
    queued = enqueue(c, 'Native service delivery fixture turn')
    await delivery._deliver(supervisor, queued['id'])
    with c.db() as db:
        message = db.get(delivery.MessageDelivery, queued['id'])
        op = service.operation(db, message.id)
        assert message.state == ('failed' if loss else 'acknowledged'), (message.error, op.receipt)
        assert op.receipt['state'] == ('uncertain' if loss else 'accepted')
        assert op.receipt['actor'] == p.to_wire(p.Actor(p.ActorKind.SERVICE, '@api', p.Channel.API))
        assert db.get(RelaySession, SID).controller['principal'] == 'alex'
        assert db.get(RelaySession, SID).control_revision == 1
        receipt = dict(op.receipt)
    for _ in range(500):
        if (await c.inspect('idle'))['idle']:
            break
        await asyncio.sleep(.02)
    else:
        raise AssertionError('Native service turn did not complete')
    assert (await c.inspect('state'))['requests'] == 1
    await delivery._deliver(supervisor, queued['id'])
    assert (await c.inspect('state'))['requests'] == 1
    with c.db() as db:
        assert service.operation(db, queued['id']).receipt == receipt
    assert len(c.wire.opens) == 1 and not c.launch.grants
    debug = await c.inspect('debug')
    assert not debug['errors']
    if loss:
        assert debug['admission_losses'] == [{'operationId': receipt['operationId'], 'outcome': 'accepted'}]
        with c.db() as db, pytest.raises(LaunchRefused):
            delivery.retry(db, db.get(delivery.MessageDelivery, queued['id']))
    else:
        assert not debug['admission_losses']
    record_property('native_service_receipt', p.canonical_json(receipt).decode())
    record_property('scope', 'actual pinned Codex/account broker/Relay/NodeLink/main; synthetic API/person/enrollment, offline model; no enrolled TLS or partition claim')
