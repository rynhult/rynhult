"""Public interrupt reauthorizes inside the actual shared native stdio lock.

Real main/broker/journal/guarded RPC composition, artificial native stream. No
native CLI, placement attestation or public enabled-path qualification is claimed.
"""
import asyncio
import time
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from test_session_relay_native_write import Writer
from test_session_relay_node_interrupts import node_interrupt  # noqa: F401
from test_session_relay_node_runtime import node_runtime  # noqa: F401
from test_session_relay_public_interrupts import interrupt_case, interrupt_reply, mutate, start  # noqa: F401
from test_session_relay_public_writer import SID, public_case  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.broker.session_relay import CodexRuntimeOwner
from acc.server.db import write_intent
from acc.server.models import (
    Node,
    Observation,
    RelayOperation,
    RelaySession,
    SecurityRevision,
    Session,
    Setting,
    utcnow,
)
from acc.session_relay.adapters.codex import StdioRPC


@pytest.mark.parametrize('change', ['target', 'policy', 'persisted-budget', 'held', 'unconfirmed', 'deadline', 'assignment',
                                    'stop', 'flag', 'capability', 'revision', 'caller', 'detach'])
async def test_public_interrupt_final_native_lock_refuses_without_cancellation_bytes(interrupt_case, change):  # noqa: F811
    await guarded_write_boundary(interrupt_case, change)


@pytest.mark.parametrize('change', ['node-status', 'node-key', 'node-account'])
async def test_node_authority_is_fresh_after_native_stdio_lock_wait(node_interrupt, change):  # noqa: F811
    await guarded_write_boundary(node_interrupt, change)


async def guarded_write_boundary(c, change):
    writer, reader = Writer(), asyncio.StreamReader()

    async def observe(_event):
        pass

    rpc = StdioRPC(reader, writer, observe)
    original_owner = c.broker_state.adapter.owner
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url='http://fixture') as client:
        view = await start(client, c)
        native_runtime = SimpleNamespace(spec=c.broker_state.spec, token='artificial-guarded-native-process', validate=lambda: None)
        c.broker_state.adapter.owner = CodexRuntimeOwner(native_runtime, rpc)
        params, identifier = await mutate(client, c, 'turn.interrupt', {'turnId': c.active,
            'expectedControlRevision': 2}, 'native-write-interrupt')
        await rpc.write_lock.acquire()
        pending = asyncio.create_task(interrupt_reply(client, {**params, 'operationId': identifier}))
        try:
            async with asyncio.timeout(10):
                while not rpc.pending:
                    if pending.done():
                        raise AssertionError(f'Interrupt settled before native write wait: {await pending}')
                    await asyncio.sleep(.01)
            assert not writer.frames

            if change == 'caller':
                c.state['revoked'] = True
            elif change == 'detach':
                from test_session_relay_public_writer import call
                await call(client, 'session.detach', {'attachmentId': view['attachmentId'], 'connectionSeq': 2})
            elif change == 'flag':
                c.ctx.settings = replace(c.ctx.settings, feature_overrides={'session_relay': False})

            @write_intent
            def revoke():
                with c.db() as db:
                    row = db.get(RelaySession, SID)
                    if change == 'node-account':
                        c.wire.link.accounts = []
                    elif change in ('node-status', 'node-key'):
                        node = db.get(Node, 'fixture-node-id')
                        if change == 'node-status':
                            node.status = 'inactive'
                        else:
                            node.public_key = 'rotated-artificial-node-key'
                    elif change == 'stop':
                        db.get(Session, SID).desired_state = 'stopped'
                    elif change == 'revision':
                        row.control_revision += 1
                    elif change == 'capability':
                        row.capabilities = {**row.capabilities, 'interrupt': 'unsupported'}
                    elif change == 'target':
                        row.active_operation_id = 'replacement-operation'
                    elif change == 'policy':
                        db.get(SecurityRevision, 1).revision += 1
                    elif change == 'persisted-budget':
                        db.add(Setting(key='budget.alex', value={'rules': {
                            'codex.primary': {'mode': 'cap', 'limit': 80, 'action': 'pause'}}}))
                        db.add(Observation(key='codex.rate_limits', source='fixture', observed_at=time.time(),
                            value={'primary': {'used_percent': 99, 'resets_at': time.time() + 3600, 'window_minutes': 300}}))
                    elif change == 'held':
                        db.get(Session, SID).desired_state = 'archived'
                        db.add(Setting(key='backup.replacement', value={'sessions': [SID]}))
                    elif change == 'unconfirmed':
                        db.get(RelayOperation, identifier).confirmed = False
                    elif change == 'deadline':
                        db.get(RelayOperation, identifier).deadline = utcnow() - timedelta(seconds=1)
                    elif change == 'assignment':
                        db.get(Session, SID).automation = {'mode': 'fixture-assignment'}
                    db.commit()
            revoke()
            rpc.write_lock.release()
            reply = await asyncio.wait_for(pending, 10)
            assert 'error' in reply or reply['result']['state'] == 'failed', reply
            assert not writer.frames and not rpc.pending
            assert c.broker_state.adapter.active_turn == c.active
            assert not c.launch.grants
            journal = c.broker_state.journal.lookup(identifier)
            assert journal is not None and journal[0] == 'rejected', journal
        finally:
            if rpc.write_lock.locked():
                rpc.write_lock.release()
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            c.broker_state.adapter.owner = original_owner
            await rpc.close()
