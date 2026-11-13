# ruff: noqa: F811
"""Journal proof can settle a lost broker answer without another native cancel."""
import asyncio
from dataclasses import replace

import httpx
import pytest
from test_session_relay_public_interrupts import interrupt_case, mutate, native_calls, start  # noqa: F401
from test_session_relay_public_writer import SID, attach, call, public_case  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.server.models import AuditLog, RelayOperation
from acc.server.session_relay.receipt_read import service


async def lost_broker_answer(client, c):
    await start(client, c)
    original = c.launch.peer.call
    async def lost(method, params):
        value = await original(method, params)
        if method == 'dispatch' and c.launch.grants[params['grant']].operation.method == 'turn.interrupt':
            raise ConnectionError('Artificial lost broker reply after durable journal settlement')
        return value
    c.launch.peer.call = lost
    payload, identifier = await mutate(client, c, 'turn.interrupt', {'turnId': c.active,
        'expectedControlRevision': 2}, 'lost-broker-answer')
    answer = await call(client, 'turn.interrupt', {**payload, 'operationId': identifier})
    assert answer['result']['state'] == 'uncertain', answer
    c.launch.peer.call = original
    assert len(native_calls(c)) == 1
    return identifier


@pytest.mark.parametrize('transport', ['rpc', 'get', 'flag_off'])
async def test_observation_uses_exact_journal_proof_without_resend(interrupt_case, transport):
    from sqlalchemy import select

    c = interrupt_case
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url='http://fixture') as client:
        identifier = await lost_broker_answer(client, c)
        if transport == 'flag_off':
            c.ctx.settings = replace(c.ctx.settings, feature_overrides={'session_relay': False})
        if transport == 'get':
            response = await client.get(f'/api/sessions/{SID}/relay/operations/{identifier}')
            assert response.status_code == 200, response.text
            receipt = response.json()
        else:
            receipt = (await call(client, 'operation.get', {'operationId': identifier}))['result']
        assert receipt['state'] == 'accepted'
        again = (await call(client, 'operation.get', {'operationId': identifier}))['result']
        assert again == receipt
        with c.db() as db:
            audit = list(db.scalars(select(AuditLog).where(AuditLog.action == 'relay.receipt.reconciled')))
            assert len(audit) == 1 and audit[0].detail['operation_id'] == identifier
    assert len(native_calls(c)) == 1 and not c.launch.grants and not service(c.launch).grants
    assert c.launch.terminal_grants.record is None and c.broker_state.adapter.active_turn == c.active


async def test_native_answer_loss_remains_unknown_even_after_observation(interrupt_case):
    c = interrupt_case
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url='http://fixture') as client:
        await start(client, c)
        original = c.owners[0].request
        async def lost(method, params):
            result = await original(method, params)
            if method == 'turn/interrupt':
                raise ConnectionError('Artificial native answer loss')
            return result
        c.owners[0].request = lost
        payload, identifier = await mutate(client, c, 'turn.interrupt', {'turnId': c.active,
            'expectedControlRevision': 2}, 'lost-native-answer')
        previous = (await call(client, 'turn.interrupt', {**payload, 'operationId': identifier}))['result']
        observed = (await call(client, 'operation.get', {'operationId': identifier}))['result']
        assert observed == previous and observed['state'] == 'uncertain'
    assert len(native_calls(c)) == 1


@pytest.mark.parametrize('change', ['detach', 'revoke', 'expiry'])
async def test_receipt_observation_rechecks_after_await(interrupt_case, change):
    c = interrupt_case
    entered, release = asyncio.Event(), asyncio.Event()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url='http://fixture') as client:
        identifier = await lost_broker_answer(client, c)
        original = c.broker_state.peer.call
        checks = 0
        async def held(method, params):
            nonlocal checks
            result = await original(method, params)
            if method == 'authority.receipt_read':
                checks += 1
                if checks == 1:
                    entered.set()
                    await release.wait()
            return result
        c.broker_state.peer.call = held
        pending = asyncio.create_task(client.post(f'/api/sessions/{SID}/relay', json={
            'jsonrpc': '2.0', 'id': 'observe', 'method': 'ry/operation.get',
            'params': {'sessionId': SID, 'operationId': identifier}}))
        await asyncio.wait_for(entered.wait(), 5)
        if change == 'revoke':
            c.state['revoked'] = True
        elif change == 'detach':
            # Attach replacement for this stable client; invalidates its token.
            await attach(client, 'interrupt-person', ['writer'])
        else:
            next(iter(service(c.launch).grants.values())).expires = 0
        release.set()
        response = await asyncio.wait_for(pending, 5)
        if change == 'revoke':
            assert response.status_code == 401
        elif change == 'detach':
            assert 'error' in response.json()
        else:
            assert response.json()['result']['state'] == 'uncertain'
        with c.db() as db:
            assert db.get(RelayOperation, identifier).receipt['state'] == 'uncertain'
    assert len(native_calls(c)) == 1 and not service(c.launch).grants


async def test_cancelled_observer_cannot_fence_shared_execution_or_replay(interrupt_case):
    c = interrupt_case
    entered, release = asyncio.Event(), asyncio.Event()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url='http://fixture') as client:
        identifier = await lost_broker_answer(client, c)
        original = c.broker_state.peer.call
        async def held(method, params):
            if method == 'authority.receipt_read' and not entered.is_set():
                entered.set()
                await release.wait()
            return await original(method, params)
        c.broker_state.peer.call = held
        pending = asyncio.create_task(call(client, 'operation.get', {'operationId': identifier}))
        await asyncio.wait_for(entered.wait(), 5)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        release.set()
        await asyncio.gather(*service(c.launch).inflight, return_exceptions=True)
        assert not c.launch.peer.closed and not c.broker_state.peer.closed
        with c.db() as db:
            assert db.get(RelayOperation, identifier).receipt['state'] == 'uncertain'
        observed = (await call(client, 'operation.get', {'operationId': identifier}))['result']
        assert observed['state'] == 'accepted'
    assert len(native_calls(c)) == 1 and not service(c.launch).grants


@pytest.mark.parametrize('proof_change', ['operation', 'kind', 'native_ref'])
async def test_unqualified_or_foreign_journal_evidence_cannot_settle(interrupt_case, proof_change):
    c = interrupt_case
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url='http://fixture') as client:
        identifier = await lost_broker_answer(client, c)
        original = c.broker_state.journal.observe
        def changed(identity):
            state, result = original(identity)
            result = {**result, 'evidence': dict(result['evidence'])}
            if proof_change == 'operation':
                result['evidence']['operation_id'] = 'foreign-operation'
            elif proof_change == 'kind':
                result['evidence']['kind'] = 'hub_commit'
            else:
                result['evidence']['native_ref'] = 'invalid\nref'
            return state, result
        c.broker_state.journal.observe = changed
        observed = (await call(client, 'operation.get', {'operationId': identifier}))['result']
        assert observed['state'] == 'uncertain'
    assert len(native_calls(c)) == 1 and not service(c.launch).grants


async def test_public_observation_reopens_durable_journal_without_replaying(interrupt_case, monkeypatch):
    """Disk reopen in a live fenced runtime; not a broker process restart."""
    from acc.broker.session_relay import AdmissionJournal
    c = interrupt_case
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url='http://fixture') as client:
        identifier = await lost_broker_answer(client, c)
        with c.db() as db:
            previous = dict(db.get(RelayOperation, identifier).receipt)
        old = c.broker_state.journal
        root = old.root
        old.close()
        reopened = AdmissionJournal(root)
        c.broker_state.journal = c.broker_state.broker.journal = reopened

        async def no_dispatch(*_args, **_kwargs):
            raise AssertionError('Receipt observation must not dispatch input after disk reopen')
        monkeypatch.setattr(c.broker_state.broker, 'dispatch', no_dispatch)
        result = await client.get(f'/api/sessions/{SID}/relay/operations/{identifier}')
        assert result.status_code == 200, result.text
        receipt = result.json()
        assert receipt['state'] == 'accepted'
        assert receipt['actor'] == previous['actor'] and receipt['payloadDigest'] == previous['payloadDigest']
        assert (await call(client, 'operation.get', {'operationId': identifier}))['result'] == receipt
    assert len(native_calls(c)) == 1 and not service(c.launch).grants


async def test_cold_main_without_runtime_observes_uncertainty_without_renewal(interrupt_case):
    c = interrupt_case
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url='http://fixture') as client:
        identifier = await lost_broker_answer(client, c)
        with c.db() as db:
            previous = dict(db.get(RelayOperation, identifier).receipt)
        assert previous['state'] == 'uncertain'
        await c.manager.close()
        async with asyncio.timeout(10):
            while c.factory.active:
                await asyncio.sleep(.01)
        c.ctx.relay_runtime = None  # Explicit missing-RAM boundary, not a process reboot.
        await attach(client, 'cold-receipt-reader', [])
        response = await client.get(f'/api/sessions/{SID}/relay/operations/{identifier}')
        assert response.status_code == 200, response.text
        assert response.json() == previous
        assert (await call(client, 'operation.get', {'operationId': identifier}))['result'] == previous
        assert c.ctx.relay_runtime is None
        assert not c.factory.active and not c.launch.grants
        assert len(c.owners) == 1 and len(native_calls(c)) == 1
