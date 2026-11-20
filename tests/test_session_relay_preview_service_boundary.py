"""Preview persons and separately gated service deliveries keep distinct policies."""
# ruff: noqa: F811

import pytest
from relay_helpers import setup
from test_session_relay_runtime import runtime_case  # noqa: F401
from test_session_relay_service_delivery import enqueue, service_case  # noqa: F401

from acc.server.api.context import Principal
from acc.server.db import write_intent
from acc.server.models import RelayOperation, RelaySession, User
from acc.server.orchestrator import delivery as queue
from acc.server.session_relay import authority, delivery
from acc.server.session_relay.hub import Hub
from acc.session_relay import protocol as p


@pytest.mark.parametrize('enabled',[False,True])
def test_service_actor_or_lookalike_scope_never_bypasses_delivery_resolver(tmp_path,monkeypatch,enabled):
    c=setup(tmp_path);monkeypatch.setattr(delivery,'DELIVERY_QUALIFIED',enabled)
    actor=p.Actor(p.ActorKind.SERVICE,'alex',p.Channel.API)
    class Lookalike:
        ctx=c.ctx
        scope={'target':'relay'}
        def in_transaction(self,db):return c.resolve()[0],actor
    try:
        for resolver in [lambda:(c.resolve()[0],actor),Lookalike()]:
            with c.db() as db,pytest.raises(p.RelayError) as error:
                authority.current(c.ctx,db,resolver,'relay',action='operation.register')
            assert error.value.code is p.ErrorCode.POLICY_REFUSED
        with c.db() as db:assert not db.query(RelayOperation).count()
    finally:c.engine.dispose()


def test_delivery_gate_does_not_exempt_a_demoted_person(tmp_path,monkeypatch):
    c=setup(tmp_path);monkeypatch.setattr(delivery,'DELIVERY_QUALIFIED',True)
    @write_intent
    def demote():
        with c.db() as db:
            db.get(User,1).role='member'
            db.get(RelaySession,'relay').capabilities={'text':'qualified','terminal.input':'qualified'}
            db.commit()
    demote();c.state['who']=Principal('alex','member','fixture',1)
    try:
        with pytest.raises(p.RelayError) as error:
            Hub(c.ctx).register(c.resolve,'relay','1','demoted','turn.submit',p.sha256_digest(b'fixture'))
        assert error.value.code is p.ErrorCode.POLICY_REFUSED
        with c.db() as db:
            assert not db.query(RelayOperation).count()
            assert db.get(RelaySession,'relay').capabilities=={}
    finally:c.engine.dispose()


async def test_scoped_delivery_uses_own_gate_without_withdrawing_person_preview(service_case,monkeypatch):
    c=service_case;message=enqueue(c,'Explicit fake gated service delivery')
    with c.db() as db:before=dict(db.get(RelaySession,'abc123def456').capabilities)
    await queue._deliver(c.service_supervisor,message['id'])
    with c.db() as db:
        operation=delivery.operation(db,message['id'])
        assert operation is not None,'Bound delivery must reach its own registration policy'
        receipt=operation.receipt
        assert receipt['state']=='accepted' and receipt['actor']['kind']=='service'
        assert receipt['actor']['principal']=='@api'
        assert db.get(RelaySession,'abc123def456').capabilities==before
    calls=list(c.owners[0].calls)
    monkeypatch.setattr(delivery,'DELIVERY_QUALIFIED',False)
    from acc.server.supervisor_common import LaunchRefused
    with pytest.raises(LaunchRefused):enqueue(c,'Must remain unavailable by default')
    assert c.owners[0].calls==calls
