"""Public interrupt receipts retain the original actor/digest and exact target."""
import asyncio
import time
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from test_session_relay_public_writer import SID, attach, call, public_case  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.server.db import write_intent
from acc.server.models import Observation, RelayOperation, RelaySession, SecurityRevision, Session, Setting, utcnow
from acc.session_relay import protocol as p


@pytest.fixture
async def interrupt_case(public_case, monkeypatch):  # noqa: F811
    return qualify_interrupt(public_case, monkeypatch)


def qualify_interrupt(c, monkeypatch):
    c.qualify_fixture()
    monkeypatch.setattr('acc.server.session_relay.writers.PUBLIC_WRITER_QUALIFIED', True)
    @write_intent
    def qualify():
        with c.db() as db:
            row = db.get(RelaySession, SID)
            row.capabilities = {**row.capabilities, 'interrupt': 'qualified'}
            db.commit()
    qualify()
    c.broker_state.adapter.record = replace(c.broker_state.adapter.record, qualification_id='synthetic-public-interrupts',
        qualification_date='2026-11-12', operations={
        **c.broker_state.adapter.record.operations,
        'text': p.CapabilityEntry(p.Support.QUALIFIED, evidence=('synthetic text only',)),
        'interrupt': p.CapabilityEntry(p.Support.QUALIFIED, evidence=('synthetic interrupt only',))})
    return c


async def mutate(client, c, method, payload, key):
    params = {'sessionId': SID, 'expectedRunId': str(c.launch.run_id), **payload}
    registered = await call(client, 'operation.register', {'expectedRunId': str(c.launch.run_id),
        'registrationKey': key, 'method': method, 'payloadDigest': p.operation_digest(method, params)})
    assert 'result' in registered, registered
    identifier = registered['result']['operationId']
    return params, identifier


async def start(client, c):
    view = await attach(client, 'interrupt-person', ['writer'])
    payload, identifier = await mutate(client, c, 'writer.take', {'view': 'chat', 'expectedRevision': 1,
        'reason': 'Person takes initial Chat', 'attachmentId': view['attachmentId']}, 'take-chat')
    assert (await call(client, 'writer.take', {**payload, 'operationId': identifier}))['result']['state'] == 'accepted'
    payload, identifier = await mutate(client, c, 'turn.submit', {'text': 'Artificial active work',
        'expectedConfigurationRevision': 0, 'expectedControlRevision': 2}, 'active-turn')
    assert (await call(client, 'turn.submit', {**payload, 'operationId': identifier}))['result']['state'] == 'accepted'
    c.active = identifier
    return view


async def interrupt_reply(client, params):
    response = await client.post(f'/api/sessions/{SID}/relay', json={
        'jsonrpc': '2.0', 'id': 'interrupt', 'method': 'ry/turn.interrupt',
        'params': {'sessionId': SID, **params}})
    assert response.status_code in (200, 401, 409), response.text
    return response.json() if response.status_code == 200 else {'error': response.json()}


def native_calls(c):
    return [(method, params) for method, params in c.owners[0].calls if method == 'turn/interrupt']


async def test_public_interrupt_requests_exact_active_turn_and_replay_only_observes(interrupt_case):
    c = interrupt_case
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url='http://fixture') as client:
        await start(client, c)
        snap = (await call(client, 'session.snapshot', {}))['result']
        assert snap['activeTurnId'] == c.active
        payload, identifier = await mutate(client, c, 'turn.interrupt', {'turnId': c.active,
            'expectedControlRevision': 2}, 'interrupt')
        reply = await call(client, 'turn.interrupt', {**payload, 'operationId': identifier})
        assert reply['result']['state'] == 'accepted', reply
        assert reply['result']['actor']['principal'] == 'alex'
        assert reply['result']['payloadDigest'] == p.operation_digest('turn.interrupt', payload)
        replay = await call(client, 'turn.interrupt', {**payload, 'operationId': identifier})
        assert replay == reply
        assert (await call(client, 'session.snapshot', {}))['result']['activeTurnId'] == c.active
    assert native_calls(c) == [('turn/interrupt', {'threadId': 'fixture-native-id',
        'turnId': c.broker_state.adapter.turns[c.active]})]
    assert not c.launch.grants


@pytest.mark.parametrize('change', ['target', 'revision', 'detach', 'stop', 'flag', 'capability', 'budget', 'deadline',
                                    'policy', 'held', 'assignment', 'unconfirmed', 'persisted-budget', 'caller'])
@pytest.mark.parametrize('boundary', ['stage', 'probe'])
async def test_final_interrupt_authority_after_await_never_cancels_replacement(interrupt_case, monkeypatch, change, boundary):
    c = interrupt_case
    entered, release = asyncio.Event(), asyncio.Event()
    original_call, original_probe = c.launch.peer.call, c.broker_state.adapter.probe
    async def paused_call(method, params):
        result = await original_call(method, params)
        if method == 'stage' and boundary == 'stage':
            entered.set()
            await release.wait()
        return result
    async def paused_probe(binding):
        result = await original_probe(binding)
        if boundary == 'probe':
            entered.set()
            await release.wait()
        return result
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url='http://fixture') as client:
        view = await start(client, c)
        payload, identifier = await mutate(client, c, 'turn.interrupt', {'turnId': c.active,
            'expectedControlRevision': 2}, 'interrupt')
        c.launch.peer.call, c.broker_state.adapter.probe = paused_call, paused_probe
        pending = asyncio.create_task(interrupt_reply(client, {**payload, 'operationId': identifier}))
        await asyncio.wait_for(entered.wait(), 5)
        if change == 'detach':
            await call(client, 'session.detach', {'attachmentId': view['attachmentId'], 'connectionSeq': 2})
        elif change == 'caller':
            c.state['revoked'] = True
        elif change == 'flag':
            c.ctx.settings = replace(c.ctx.settings, feature_overrides={'session_relay': False})
        elif change == 'budget':
            monkeypatch.setattr('acc.server.session_relay.runtime.budgets.paused', lambda *_a, **_kw: True)
        else:
            @write_intent
            def revoke():
                with c.db() as db:
                    row = db.get(RelaySession, SID)
                    if change == 'target':
                        row.active_operation_id = 'replacement-operation'
                    elif change == 'revision':
                        row.control_revision += 1
                    elif change == 'stop':
                        db.get(Session, SID).desired_state = 'stopped'
                    elif change == 'capability':
                        row.capabilities = {**row.capabilities, 'interrupt': 'unsupported'}
                    elif change == 'policy':
                        db.get(SecurityRevision, 1).revision += 1
                    elif change == 'held':
                        db.get(Session, SID).desired_state = 'archived'
                        db.add(Setting(key='backup.replacement', value={'sessions': [SID]}))
                    elif change == 'assignment':
                        db.get(Session, SID).automation = {'mode': 'fixture-assignment'}
                    elif change == 'unconfirmed':
                        db.get(RelayOperation, identifier).confirmed = False
                    elif change == 'persisted-budget':
                        db.add(Setting(key='budget.alex', value={'rules': {
                            'codex.primary': {'mode': 'cap', 'limit': 80, 'action': 'pause'}}}))
                        db.add(Observation(key='codex.rate_limits', source='fixture', observed_at=time.time(),
                            value={'primary': {'used_percent': 99, 'resets_at': time.time() + 3600, 'window_minutes': 300}}))
                    elif change == 'deadline':
                        db.get(RelayOperation, identifier).deadline = utcnow() - timedelta(seconds=1)
                        # The immutable grant carries its original deadline too;
                        # both durable and grant deadlines are final fences.
                    db.commit()
            revoke()
        release.set()
        reply = await asyncio.wait_for(pending, 5)
        assert 'error' in reply or reply['result']['state'] == 'failed', reply
    assert not native_calls(c)
    assert c.broker_state.adapter.active_turn == c.active
    assert not c.launch.grants


async def test_lost_interrupt_answer_is_uncertain_and_duplicate_does_not_resend(interrupt_case):
    c = interrupt_case
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url='http://fixture') as client:
        await start(client, c)
        original = c.owners[0].request
        async def lost(method, params):
            value = await original(method, params)
            if method == 'turn/interrupt':
                raise ConnectionError('Artificial lost native answer')
            return value
        c.owners[0].request = lost
        payload, identifier = await mutate(client, c, 'turn.interrupt', {'turnId': c.active,
            'expectedControlRevision': 2}, 'interrupt')
        first = await call(client, 'turn.interrupt', {**payload, 'operationId': identifier})
        assert first['result']['state'] == 'uncertain', first
        assert await call(client, 'turn.interrupt', {**payload, 'operationId': identifier}) == first
        assert len(native_calls(c)) == 1


async def test_public_interrupt_default_gate_refuses_registration(interrupt_case, monkeypatch):
    c = interrupt_case
    monkeypatch.setattr('acc.server.session_relay.writers.PUBLIC_WRITER_QUALIFIED', False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url='http://fixture') as client:
        await attach(client, 'unqualified-interrupt')
        reply = await call(client, 'operation.register', {'expectedRunId': str(c.launch.run_id),
            'registrationKey': 'closed', 'method': 'turn.interrupt', 'payloadDigest': p.sha256_digest(b'fixture')})
        assert reply['error']['data']['code'] == 'UNSUPPORTED'
    assert not native_calls(c)


async def test_other_attachment_cannot_interrupt_current_writer_even_for_same_person(interrupt_case):
    c = interrupt_case
    transport = httpx.ASGITransport(c.app)
    async with httpx.AsyncClient(transport=transport, base_url='http://fixture') as writer, httpx.AsyncClient(transport=transport, base_url='http://fixture') as observer:
        await start(writer, c)
        await attach(observer, 'other-person-tab', ['writer'])
        payload, identifier = await mutate(observer, c, 'turn.interrupt', {'turnId': c.active,
            'expectedControlRevision': 2}, 'observer-interrupt')
        reply = await call(observer, 'turn.interrupt', {**payload, 'operationId': identifier})
        assert reply['error']['data']['code'] == 'STALE_AUTHORITY'
    assert not native_calls(c) and c.broker_state.adapter.active_turn == c.active


@pytest.mark.parametrize('revision', [None, 1, 3])
async def test_interrupt_needs_exact_control_revision(interrupt_case, revision):
    c = interrupt_case
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url='http://fixture') as client:
        await start(client, c)
        params = {'turnId': c.active, **({'expectedControlRevision': revision} if revision is not None else {})}
        payload, identifier = await mutate(client, c, 'turn.interrupt', params, 'wrong-revision')
        reply = await call(client, 'turn.interrupt', {**payload, 'operationId': identifier})
        assert reply['error']['data']['code'] == 'STALE_AUTHORITY'
    assert not native_calls(c)


async def test_cancelled_probe_stays_uncertain_and_never_replays(interrupt_case):
    c = interrupt_case
    entered, release = asyncio.Event(), asyncio.Event()
    original = c.broker_state.adapter.probe
    async def paused(binding):
        result = await original(binding)
        entered.set()
        await release.wait()
        return result
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url='http://fixture') as client:
        await start(client, c)
        payload, identifier = await mutate(client, c, 'turn.interrupt', {'turnId': c.active,
            'expectedControlRevision': 2}, 'cancelled-interrupt')
        c.broker_state.adapter.probe = paused
        pending = asyncio.create_task(interrupt_reply(client, {**payload, 'operationId': identifier}))
        await asyncio.wait_for(entered.wait(), 5)
        pending.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(pending, 5)
        finally:
            release.set()
        for _ in range(100):
            if not c.manager.background:
                break
            await asyncio.sleep(.01)
        with c.db() as db:
            assert db.get(RelayOperation, identifier).receipt['state'] == 'uncertain'
    assert not native_calls(c) and not c.launch.grants


@pytest.mark.parametrize('changed', ['confirmation', 'deadline'])
async def test_settled_interrupt_replay_remains_observation_after_registration_expires(interrupt_case, changed):
    c = interrupt_case
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url='http://fixture') as client:
        await start(client, c)
        params, identifier = await mutate(client, c, 'turn.interrupt', {'turnId': c.active,
            'expectedControlRevision': 2}, 'settled-observation')
        first = await call(client, 'turn.interrupt', {**params, 'operationId': identifier})
        assert first['result']['state'] == 'accepted' and len(native_calls(c)) == 1

        @write_intent
        def expire():
            with c.db() as db:
                record = db.get(RelayOperation, identifier)
                if changed == 'confirmation':
                    record.confirmed = False
                else:
                    record.deadline = utcnow() - timedelta(seconds=1)
                db.commit()
        expire()
        observed = await call(client, 'turn.interrupt', {**params, 'operationId': identifier})
        assert observed == first
        assert len(native_calls(c)) == 1 and not c.launch.grants
