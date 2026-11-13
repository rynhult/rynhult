"""Queued public interruption rechecks authority after the real submit-lock wait."""
import asyncio
import time
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from test_session_relay_node_interrupts import node_interrupt  # noqa: F401
from test_session_relay_node_runtime import node_runtime  # noqa: F401
from test_session_relay_public_interrupts import (  # noqa: F401
    interrupt_case,
    interrupt_reply,
    mutate,
    native_calls,
    start,
)
from test_session_relay_public_writer import SID, call, public_case  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401

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


@pytest.mark.parametrize('change', ['policy', 'persisted-budget', 'held', 'target', 'unconfirmed', 'detach', 'caller', 'deadline',
                                    'stop', 'flag', 'capability', 'revision', 'assignment'])
async def test_waiting_public_interrupt_never_borrows_prequeue_authority(interrupt_case, change, monkeypatch):  # noqa: F811
    await queued_boundary(interrupt_case, change, monkeypatch)


@pytest.mark.parametrize('change', ['node-status', 'node-key', 'node-account'])
async def test_waiting_node_interrupt_never_borrows_prequeue_authority(node_interrupt, change, monkeypatch):  # noqa: F811
    await queued_boundary(node_interrupt, change, monkeypatch)


async def queued_boundary(c, change, monkeypatch):
    underlying = c.launch.submit_lock
    waiting = asyncio.Event()

    class ObservedLock:
        async def __aenter__(self):
            waiting.set()
            await underlying.acquire()
            return self
        async def __aexit__(self, *_):
            underlying.release()
        def locked(self):
            return underlying.locked()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url='http://fixture') as client:
        view = await start(client, c)
        params, identifier = await mutate(client, c, 'turn.interrupt', {'turnId': c.active,
            'expectedControlRevision': 2}, 'queued-interrupt')
        await underlying.acquire()
        monkeypatch.setattr(c.launch, 'submit_lock', ObservedLock())
        pending = asyncio.create_task(interrupt_reply(client, {**params, 'operationId': identifier}))
        try:
            await asyncio.wait_for(waiting.wait(), 10)
            assert underlying.locked() and not pending.done()
            assert c.broker_state.journal.lookup(identifier) is None and not native_calls(c)
            if change == 'detach':
                await call(client, 'session.detach', {'attachmentId': view['attachmentId'], 'connectionSeq': 2})
            elif change == 'caller':
                c.state['revoked'] = True
            elif change == 'flag':
                c.ctx.settings = replace(c.ctx.settings, feature_overrides={'session_relay': False})
            elif change == 'node-account':
                c.wire.link.accounts = []
            else:
                @write_intent
                def revoke():
                    with c.db() as db:
                        if change in ('node-status', 'node-key'):
                            node = db.get(Node, 'fixture-node-id')
                            if change == 'node-status':
                                node.status = 'inactive'
                            else:
                                node.public_key = 'rotated-artificial-node-key'
                        elif change == 'stop':
                            db.get(Session, SID).desired_state = 'stopped'
                        elif change == 'revision':
                            db.get(RelaySession, SID).control_revision += 1
                        elif change == 'capability':
                            row = db.get(RelaySession, SID)
                            row.capabilities = {**row.capabilities, 'interrupt': 'unsupported'}
                        elif change == 'assignment':
                            db.get(Session, SID).automation = {'mode': 'fixture-assignment'}
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
                        elif change == 'deadline':
                            db.get(RelayOperation, identifier).deadline = utcnow() - timedelta(seconds=1)
                        elif change == 'target':
                            db.get(RelaySession, SID).active_operation_id = 'replacement-operation'
                        else:
                            db.get(RelayOperation, identifier).confirmed = False
                        db.commit()
                revoke()
            underlying.release()
            reply = await asyncio.wait_for(pending, 10)
            assert 'error' in reply or reply['result']['state'] == 'failed', reply
            if change in ('unconfirmed', 'deadline'):
                assert reply['error']['data']['code'] == 'STALE_AUTHORITY', reply
            assert c.broker_state.journal.lookup(identifier) is None
            assert not native_calls(c) and not c.launch.grants
            assert c.broker_state.adapter.active_turn == c.active
        finally:
            if underlying.locked():
                underlying.release()
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
