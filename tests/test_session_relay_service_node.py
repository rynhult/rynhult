"""Scoped delivery rechecks the current enrolled node identity/account/link."""
import pytest
from test_session_relay_node_runtime import HOST, node_runtime  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401
from test_session_relay_service_delivery import SID, configure, enqueue

from acc.server.db import write_intent
from acc.server.models import Node
from acc.server.orchestrator import delivery
from acc.server.session_relay import delivery as service


@pytest.mark.parametrize('change', ['none', 'node-key', 'account'])
async def test_service_delivery_over_typed_enrolled_node_rechecks_identity(node_runtime, monkeypatch, change):  # noqa: F811
    c = node_runtime
    await c.launch.launch(SID)
    configure(c, monkeypatch)
    queued = enqueue(c)
    if change == 'node-key':
        @write_intent
        def rotate():
            with c.db() as db:
                node = db.query(Node).filter_by(name=HOST).one()
                node.public_key = 'synthetic-replacement-key'
                db.commit()
        rotate()
    elif change == 'account':
        c.wire.link.accounts = []
    await delivery._deliver(c.service_supervisor, queued['id'])
    with c.db() as db:
        op = service.operation(db, queued['id'])
        assert op.receipt['state'] == ('accepted' if change == 'none' else 'failed')
        assert op.receipt['actor']['kind'] == 'service'
    assert sum(method == 'turn/start' for method, _ in c.owners[0].calls) == (1 if change == 'none' else 0)
    assert len(c.wire.opens) == 1
