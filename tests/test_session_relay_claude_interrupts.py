# ruff: noqa: F811
"""Claude public exact-query interruption with real SDK and artificial CLI."""
import asyncio

import pytest
from test_session_relay_claude_public import mutate, public_claude  # noqa: F401
from test_session_relay_ownership import ownership_case  # noqa: F401
from test_session_relay_public_writer import SID, attach, call
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.server.db import write_intent
from acc.server.models import RelayOperation, RelaySession
from acc.session_relay import protocol as p


@pytest.fixture
async def claude_interrupt(public_claude, monkeypatch):
    c = public_claude
    c.enable()
    monkeypatch.setattr('acc.server.session_relay.interrupts.PUBLIC_CLAUDE_INTERRUPTS_QUALIFIED', True)
    monkeypatch.setattr('acc.server.session_relay.writers.PUBLIC_WRITER_QUALIFIED', True)
    @write_intent
    def qualify():
        with c.db() as db:
            row = db.get(RelaySession, SID)
            row.capabilities = {**row.capabilities, 'interrupt': 'qualified'}
            db.commit()
    qualify()
    await attach(c.client, 'claude-interrupt-person', ['writer', 'switch'])
    with c.db() as db:
        c.control = db.get(RelaySession, SID).control_revision
    reply, c.active = await mutate(c, 'turn.submit', {'text': 'wait-fixture', 'expectedConfigurationRevision': 0,
        'expectedControlRevision': c.control}, 'active-sdk-query')
    assert reply['result']['state'] == 'uncertain', reply
    c.adapter = c.owned_factory.current
    return c


async def register(c, target, key):
    params = {'sessionId': SID, 'expectedRunId': str(c.launch.run_id), 'turnId': target, 'expectedControlRevision': c.control}
    registered = await call(c.client, 'operation.register', {'expectedRunId': str(c.launch.run_id),
        'registrationKey': key, 'method': 'turn.interrupt', 'payloadDigest': p.operation_digest('turn.interrupt', params)})
    assert 'result' in registered, registered
    return {**params, 'operationId': registered['result']['operationId']}


async def test_public_sdk_interrupt_is_requested_not_confirmed_and_replay_cannot_cancel_next_query(claude_interrupt):
    c = claude_interrupt
    params = await register(c, c.active, 'first-interrupt')
    reply = await call(c.client, 'turn.interrupt', params)
    assert 'result' in reply, reply
    assert reply['result']['state'] == 'accepted', reply
    assert reply['result']['actor']['principal'] == 'alex'
    wire = c.adapter.client._transport
    assert len(wire.interrupts) == 1
    assert all(v['writeCompleted'] and v['acknowledged'] and not v['ambiguous'] for v in wire.interrupts.values())
    for _ in range(300):
        with c.db() as db:
            done = db.get(RelaySession, SID).active_operation_id is None
        if done:
            break
        await asyncio.sleep(.01)
    assert done
    second, new_active = await mutate(c, 'turn.submit', {'text': 'wait-fixture replacement',
        'expectedConfigurationRevision': 0, 'expectedControlRevision': c.control}, 'replacement-query')
    assert second['result']['state'] == 'uncertain' and new_active != c.active
    assert await call(c.client, 'turn.interrupt', params) == reply
    stale = await register(c, c.active, 'stale-query-interrupt')
    refused = await call(c.client, 'turn.interrupt', stale)
    assert refused['error']['data']['code'] == 'STALE_AUTHORITY', refused
    assert c.adapter.active and c.adapter.applications.current['operation'].operation_id == new_active
    assert len(wire.interrupts) == 1 and not c.launch.grants


@pytest.mark.parametrize('boundary', ['stage', 'pipe-lock'])
@pytest.mark.parametrize('change', ['revision', 'target', 'capability', 'confirmation'])
async def test_final_sdk_interrupt_fence_after_wait_never_writes(claude_interrupt, boundary, change):
    c = claude_interrupt
    params = await register(c, c.active, 'revoked-interrupt')
    entered, release = asyncio.Event(), asyncio.Event()
    original = c.launch.peer.call
    async def paused(method, values):
        result = await original(method, values)
        if method == 'stage':
            entered.set()
            await release.wait()
        return result
    wire = c.adapter.client._transport
    if boundary == 'stage':
        c.launch.peer.call = paused
    else:
        await wire.base._write_lock.acquire()
    pending = asyncio.create_task(call(c.client, 'turn.interrupt', params))
    try:
        if boundary == 'stage':
            await asyncio.wait_for(entered.wait(), 5)
        else:
            for _ in range(300):
                if wire.interrupts:
                    break
                await asyncio.sleep(.01)
            assert wire.interrupts and not next(iter(wire.interrupts.values()))['writeCompleted']
        @write_intent
        def revoke():
            with c.db() as db:
                row = db.get(RelaySession, SID)
                if change == 'revision':
                    row.control_revision += 1
                elif change == 'target':
                    row.active_operation_id = 'replacement-operation'
                elif change == 'capability':
                    row.capabilities = {**row.capabilities, 'interrupt': 'unsupported'}
                else:
                    db.get(RelayOperation, params['operationId']).confirmed = False
                db.commit()
        revoke()
    finally:
        release.set()
        if boundary == 'pipe-lock':
            wire.base._write_lock.release()
    reply = await asyncio.wait_for(pending, 5)
    assert 'error' in reply or reply['result']['state'] == 'failed', reply
    assert all(not v['writeCompleted'] and not v['acknowledged'] for v in wire.interrupts.values())
    assert not c.launch.grants


async def test_sdk_lost_dispatch_answer_observes_original_journal_without_replay(claude_interrupt):
    c = claude_interrupt
    params = await register(c, c.active, 'lost-answer-interrupt')
    original = c.launch.peer.call
    async def lose(method, values):
        result = await original(method, values)
        if method == 'dispatch':
            raise ConnectionError('Artificial lost broker dispatch answer')
        return result
    c.launch.peer.call = lose
    reply = await call(c.client, 'turn.interrupt', params)
    assert reply['result']['state'] == 'uncertain', reply
    wire = c.adapter.client._transport
    assert len(wire.interrupts) == 1
    observed = await c.client.get(f"/api/sessions/{SID}/relay/operations/{params['operationId']}")
    assert observed.status_code == 200, observed.text
    accepted = observed.json()
    assert accepted['state'] == 'accepted' and accepted['operationId'] == params['operationId']
    assert accepted['actor'] == reply['result']['actor'] and accepted['payloadDigest'] == reply['result']['payloadDigest']
    assert (await call(c.client, 'turn.interrupt', params))['result'] == accepted
    assert len(wire.interrupts) == 1 and not c.launch.grants


async def test_sdk_interrupt_default_qualification_gate_stays_closed(claude_interrupt, monkeypatch):
    c = claude_interrupt
    monkeypatch.setattr('acc.server.session_relay.interrupts.PUBLIC_CLAUDE_INTERRUPTS_QUALIFIED', False)
    result = await call(c.client, 'operation.register', {'expectedRunId': str(c.launch.run_id),
        'registrationKey': 'closed-sdk-interrupt', 'method': 'turn.interrupt', 'payloadDigest': p.sha256_digest(b'fixture')})
    assert result['error']['data']['code'] == 'UNSUPPORTED'
    assert not c.adapter.client._transport.interrupts and c.adapter.active


async def test_other_attachment_for_same_person_cannot_interrupt_sdk_query(claude_interrupt):
    c = claude_interrupt
    await attach(c.client, 'another-person-tab', ['writer', 'switch'])
    params = await register(c, c.active, 'foreign-tab-interrupt')
    result = await call(c.client, 'turn.interrupt', params)
    assert result['error']['data']['code'] == 'STALE_AUTHORITY', result
    assert not c.adapter.client._transport.interrupts and c.adapter.active
    with c.db() as db:
        assert db.get(RelaySession, SID).active_operation_id == c.active
