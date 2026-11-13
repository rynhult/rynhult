"""Typed whole-tree acknowledgements; fixtures do not qualify native execution."""
import pytest
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.server.db import write_intent
from acc.server.models import RelayRun, Session
from acc.server.session_relay import stop_proof
from acc.server.session_relay.hub import seal_for_stop
from acc.server.session_relay.runtime import RuntimeManager

SID = 'abc123def456'


@pytest.mark.parametrize('reply', [{'exited': True}, {'exited': True, 'wholeTree': 'root-only'},
    {'exited': 1, 'wholeTree': 'enclosure-v1'}, {'exited': False, 'wholeTree': 'enclosure-v1'},
    {'exited': True, 'wholeTree': 'enclosure-v1', 'extra': 1}, 'disconnect', 'real'])
async def test_codex_stop_requires_whole_tree_before_durable_retirement(runtime_case, reply):  # noqa: F811
    c = runtime_case
    await c.launch.launch(SID)
    @write_intent
    def stop_intent():
        seal_for_stop(c.ctx, SID)
        with c.db() as db:
            db.get(Session, SID).desired_state = 'stopped'
            db.commit()
    stop_intent()
    original = c.launch.peer.call
    async def call(method, params):
        if method != 'stop' or reply == 'real':
            return await original(method, params)
        if reply == 'disconnect':
            raise ConnectionError('fixture lost ack')
        return reply
    c.launch.peer.call = call
    expected = reply == 'real'
    assert await c.manager.stop(SID) is expected
    assert await c.manager.stop(SID) is expected
    fresh = RuntimeManager(c.ctx)
    try:
        assert await fresh.stop(SID) is expected
        assert not fresh.runs and not fresh.launches
    finally:
        await fresh.close()
    with c.db() as db:
        assert (stop_proof.KEY in db.get(RelayRun, c.launch.run_id).identity) is expected
