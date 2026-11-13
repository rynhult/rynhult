"""Public exact-turn interruption over a real typed NodeLink and private broker.

Node wire/process and authentication are fixtures, not a live node or native CLI.
Revoking node metadata/account access must win despite a still-open stream.
"""
import asyncio

import httpx
import pytest
from fastapi import FastAPI, Request
from test_session_relay_node_runtime import HOST, SID, node_runtime  # noqa: F401
from test_session_relay_public_interrupts import interrupt_reply, mutate, native_calls, qualify_interrupt, start
from test_session_relay_runtime import runtime_case  # noqa: F401
from test_session_relay_writer_routes import retire

from acc.server.api.session_relay import router
from acc.server.db import write_intent
from acc.server.models import Node, RelaySession


@pytest.fixture
async def node_interrupt(node_runtime, monkeypatch):  # noqa: F811
    c = node_runtime
    await c.launch.launch(SID)
    c.ctx.relay_runtime = c.manager

    def principal(request: Request):
        request.state.cookie_authenticated = True
        request.state.session_hash = 'fixture-node-login'
        return c.resolve()[0]

    c.ctx.principal = principal
    c.ctx.relay_principal = lambda _db, request: principal(request)
    c.app = FastAPI()
    c.app.include_router(router(c.ctx))
    c.broker_state = next(iter(c.factory.active.values()))
    c.broker_state.writers.retire = retire  # explicit synthetic native retirement

    @write_intent
    def qualify():
        with c.db() as db:
            db.get(RelaySession, SID).capabilities = {'text': 'qualified', 'terminal.input': 'qualified'}
            db.commit()

    c.qualify_fixture = qualify
    return qualify_interrupt(c, monkeypatch)


@pytest.mark.parametrize('boundary', ['stage', 'probe'])
@pytest.mark.parametrize('change', ['node-status', 'node-key', 'node-account'])
async def test_still_open_node_stream_cannot_cancel_after_authority_change(node_interrupt, boundary, change):
    c = node_interrupt
    entered, release = asyncio.Event(), asyncio.Event()
    original_call, original_probe = c.launch.peer.call, c.broker_state.adapter.probe

    async def staged(method, params):
        value = await original_call(method, params)
        if method == 'stage' and boundary == 'stage':
            entered.set()
            await release.wait()
        return value

    async def probed(binding):
        value = await original_probe(binding)
        if boundary == 'probe':
            entered.set()
            await release.wait()
        return value

    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url='http://fixture') as client:
        await start(client, c)
        params, identifier = await mutate(client, c, 'turn.interrupt', {'turnId': c.active,
            'expectedControlRevision': 2}, 'node-interrupt')
        c.launch.peer.call, c.broker_state.adapter.probe = staged, probed
        pending = asyncio.create_task(interrupt_reply(client, {**params, 'operationId': identifier}))
        try:
            await asyncio.wait_for(entered.wait(), 10)
            assert not c.wire.link.closed
            if change == 'node-account':
                c.wire.link.accounts = []
            else:
                @write_intent
                def revoke():
                    with c.db() as db:
                        node = db.get(Node, 'fixture-node-id')
                        assert node.name == HOST
                        if change == 'node-status':
                            node.status = 'inactive'
                        else:
                            node.public_key = 'rotated-artificial-node-key'
                        db.commit()
                revoke()
            assert not c.wire.link.closed  # Metadata revocation, not a broken socket.
        finally:
            release.set()
        reply = await asyncio.wait_for(pending, 10)
        assert 'error' in reply or reply['result']['state'] == 'failed', reply
    assert not native_calls(c)
    assert c.broker_state.adapter.active_turn == c.active
    assert len(c.wire.opens) == 1 and not c.launch.grants
