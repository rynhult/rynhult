"""Coalesced SCHEDULE notice through actual OS broker/native owner/Relay/NodeLink."""

import asyncio

import pytest
from test_session_relay_node_runtime import node_runtime  # noqa: F401
from test_session_relay_process_restart import process_node  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.server.db import write_intent
from acc.server.models import AuditLog, RelaySession, Session, SessionRun, Team
from acc.server.orchestrator import delivery
from acc.server.session_relay import delivery as service
from acc.session_relay import protocol as p

SID = 'abc123def456'


pytestmark = pytest.mark.relay_native


async def test_native_coalesced_notice_keeps_intents_and_one_schedule_submission(process_node, monkeypatch, record_property):  # noqa: F811
    c = process_node
    monkeypatch.setattr(service, 'DELIVERY_QUALIFIED', True)
    monkeypatch.setattr('acc.server.session_relay.writers.PUBLIC_WRITER_QUALIFIED', True)
    monkeypatch.setattr(delivery, 'enabled', lambda: True)
    @write_intent
    def reserve_team():
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
    reserve_team()
    await c.launch.launch(SID)
    await c.inspect('qualify')
    c.ctx.relay_runtime = c.manager
    supervisor = c.ctx.supervisor
    supervisor.db, supervisor.relay_manager, supervisor.broker = c.db, c.manager, c.ctx.broker
    supervisor._get = lambda db, identifier: db.get(Session, identifier)
    @write_intent
    def notices():
        with c.db() as db:
            db.get(RelaySession, SID).capabilities = {'text': 'qualified', 'terminal.input': 'qualified'}
            first = delivery.nudge_lead(db, SID, 'plan')
            first_id = first.id
            second = delivery.nudge_lead(db, SID, 'answer')
            second_id = second.id
            assert first_id != second_id
            assert 'edited the plan' in second.body and 'answered a card' in second.body
            db.commit()
            return first_id, second_id
    first, second = notices()
    assert (await c.inspect('state'))['requests'] == 0
    await delivery._deliver(supervisor, second)
    with c.db() as db:
        old = db.get(delivery.MessageDelivery, first)
        op = service.operation(db, second)
        assert old.state == 'expired' and service.operation(db, first) is None
        assert op.receipt['state'] == 'accepted'
        assert op.receipt['actor'] == p.to_wire(p.Actor(p.ActorKind.SERVICE, 'supervisor', p.Channel.SCHEDULE))
        assert op.authority['delivery']['notice']['family'] == 'lead'
        assert db.get(RelaySession, SID).control_revision == 1
        assert db.get(RelaySession, SID).controller['principal'] == 'alex'
        audit = db.query(AuditLog).filter_by(action='relay.delivery.settled').one()
        assert audit.actor == 'supervisor' and audit.detail['channel'] == 'schedule'
        assert audit.detail['previous']['principal'] == 'alex'
        receipt = dict(op.receipt)
    for _ in range(500):
        if (await c.inspect('idle'))['idle']:
            break
        await asyncio.sleep(.02)
    else:
        raise AssertionError('Native notice turn did not finish')
    await delivery._deliver(supervisor, first)
    await delivery._deliver(supervisor, second)
    assert (await c.inspect('state'))['requests'] == 1
    assert not c.launch.grants
    with c.db() as db:
        assert service.operation(db, second).receipt == receipt
    record_property('notice_receipt', p.canonical_json(receipt).decode())
    record_property('native_requests', 1)
    record_property('scope', 'real pinned Codex/OS account broker/production Relay and NodeLink; synthetic team/person/enrollment, offline model; no TLS or browser claim')
