"""Production grant/byte dispatch composition with an artificial Host, never native attestation."""
import asyncio
import base64
import time

import httpx
import pytest
from test_session_relay_public_writer import SID, attach, call, public_case, register  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.server.models import RelaySession
from acc.session_relay import protocol as p
from acc.session_relay.adapters.base import TerminalRun, Watermark, WriteResult


@pytest.fixture
async def stream_case(public_case, monkeypatch):  # noqa: F811
    case = public_case
    case.qualify_fixture()
    monkeypatch.setattr('acc.server.session_relay.writers.PUBLIC_WRITER_QUALIFIED', True)
    class Host:
        def __init__(self):
            self.run = TerminalRun(SID, str(case.launch.run_id))
            self.claims = []
            self.writes = []
            self.block = None
        async def detach(self, run):
            assert run == self.run
        async def claim_input(self, grant):
            assert await case.broker_state.terminal_value(grant.grant, 'held') == grant
            self.claims.append(grant)
        async def write_frames(self, grant, sequence, data):
            assert await case.broker_state.terminal_value(grant.grant, 'held') == grant
            self.writes.append((sequence, data))
            if self.block:
                self.block[0].set()
                await self.block[1].wait()
            return WriteResult(Watermark(grant.epoch, sequence, 0))
    case.host = Host()
    case.broker_state.terminal_host = case.host
    async with httpx.AsyncClient(transport=httpx.ASGITransport(case.app), base_url='http://fixture') as client:
        view = await attach(client, 'stream-holder', ['writer', 'terminal'])
        params = {'expectedRunId': str(case.launch.run_id), 'attachmentId': view['attachmentId'],
                  'expectedRevision': 1, 'view': 'terminal', 'reason': 'Artificial explicit Terminal take'}
        operation = (await register(client, case, params, 'take-terminal'))['result']['operationId']
        assert (await call(client, 'writer.take', {**params, 'operationId': operation}))['result']['state'] == 'accepted'
        case.client, case.attachment = client, view['attachmentId']
        yield case


async def claim(case, key='claim'):
    params = {'sessionId': SID, 'expectedRunId': str(case.launch.run_id), 'attachmentId': case.attachment, 'expectedRevision': 2}
    registration = (await call(case.client, 'operation.register', {'expectedRunId': str(case.launch.run_id),
        'registrationKey': key, 'method': 'control.claim', 'payloadDigest': p.operation_digest('control.claim', params)}))['result']
    return await call(case.client, 'control.claim', {**params, 'operationId': registration['operationId']})


def frame(case, grant, sequence=1):
    return {'attachmentId': case.attachment, 'inputGrant': grant['inputGrant'], 'inputEpoch': grant['inputEpoch'],
            'frameSeq': sequence, 'data': base64.b64encode(b'artificial bytes').decode()}


async def test_only_explicit_claim_mints_stream_and_native_delivery_is_not_processing(stream_case):
    case = stream_case
    assert case.launch.terminal_grants.record is None
    grant = (await claim(case))['result']
    assert grant['receipt']['state'] == 'accepted'
    assert grant['inputEpoch'] == 2 and len(grant['inputGrant']) == 64
    assert grant['inputGrant'] not in repr(case.host.claims[0])
    reply = (await call(case.client, 'terminal.input', frame(case, grant)))['result']
    assert reply['watermark'] == {'epoch': 2, 'admitted': 1, 'processed': 0}
    assert case.host.writes == [(1, b'artificial bytes')]
    assert (await call(case.client, 'session.snapshot', {}))['result']['terminalMediated'] is False
    # Delivered unsent text may retire at idle; this is not a processing/drain proof.
    params = {'expectedRunId': str(case.launch.run_id), 'attachmentId': case.attachment,
              'expectedRevision': 2, 'view': 'chat', 'reason': 'Artificial premature transfer'}
    operation = (await register(case.client, case, params, 'no-drain'))['result']['operationId']
    result = await call(case.client, 'writer.take', {**params, 'operationId': operation})
    assert result['result']['state'] == 'accepted'
    assert case.broker_state.writers.gate.epoch == 3
    assert 'error' in await call(case.client, 'terminal.input', frame(case, grant, 2))
    assert case.host.writes == [(1, b'artificial bytes')]


async def test_grant_has_no_fallback_for_wrong_token_epoch_sequence_or_other_tab(stream_case):
    case = stream_case
    grant = (await claim(case))['result']
    good = frame(case, grant)
    for changes in ({'inputGrant': 'unknown'}, {'inputEpoch': 1}, {'frameSeq': 2}):
        assert 'error' in await call(case.client, 'terminal.input', {**good, **changes})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(case.app), base_url='http://fixture') as other:
        view = await attach(other, 'other-stream-view', ['writer', 'terminal'])
        assert 'error' in await call(other, 'terminal.input', {**good, 'attachmentId': view['attachmentId']})
    assert case.host.writes == []
    assert 'result' in await call(case.client, 'terminal.input', good)
    assert 'error' in await call(case.client, 'terminal.input', good)  # frame cannot replay
    assert len(case.host.writes) == 1


async def test_claim_replay_never_renews_or_remints_stream(stream_case):
    case = stream_case
    first = (await claim(case))['result']
    repeated = (await claim(case))['result']
    assert repeated['receipt']['operationId'] == first['receipt']['operationId']
    assert 'inputGrant' not in repeated and len(case.host.claims) == 1
    assert 'error' in await claim(case, 'second-claim-same-epoch')
    assert len(case.host.claims) == 1


async def test_expired_or_disconnected_stream_never_delivers(stream_case):
    case = stream_case
    grant = (await claim(case))['result']
    case.launch.terminal_grants.record.expires = time.monotonic() - 1
    assert 'error' in await call(case.client, 'terminal.input', frame(case, grant))
    assert not case.host.writes


async def test_disconnect_during_host_write_seals_without_replay_or_false_ack(stream_case):
    case = stream_case
    grant = (await claim(case))['result']
    reached, release = asyncio.Event(), asyncio.Event()
    case.host.block = reached, release
    pending = asyncio.create_task(call(case.client, 'terminal.input', frame(case, grant)))
    await asyncio.wait_for(reached.wait(), 5)
    await call(case.client, 'session.detach', {'attachmentId': case.attachment, 'connectionSeq': 2})
    release.set()
    assert 'error' in await asyncio.wait_for(pending, 5)
    assert len(case.host.writes) == 1
    with case.db() as db:
        assert db.get(RelaySession, SID).sealed
    assert case.launch.terminal_grants.record is None


async def test_cancelled_input_never_restores_stream_or_replays_bytes(stream_case):
    case = stream_case
    grant = (await claim(case))['result']
    reached, release = asyncio.Event(), asyncio.Event()
    case.host.block = reached, release
    pending = asyncio.create_task(call(case.client, 'terminal.input', frame(case, grant)))
    await asyncio.wait_for(reached.wait(), 5)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    release.set()
    assert len(case.host.writes) == 1
    assert case.launch.terminal_grants.record is None
    with case.db() as db:
        assert db.get(RelaySession, SID).sealed


async def test_unexpected_host_ack_fields_cannot_be_exposed_or_count_as_processing(stream_case):
    case = stream_case
    grant = (await claim(case))['result']
    async def malformed_ack(value, sequence, data):
        case.host.writes.append((sequence, data))
        return {'partial': False, 'watermark': {'epoch': value.epoch, 'admitted': sequence, 'processed': sequence},
                'private': 'fixture-private-data'}
    case.host.write_frames = malformed_ack
    result = await call(case.client, 'terminal.input', frame(case, grant))
    assert result['error']['data']['code'] == 'INTEGRITY_FAILURE'
    assert 'fixture-private-data' not in str(result)
    with case.db() as db:
        assert db.get(RelaySession, SID).sealed


@pytest.mark.parametrize('data', [b'/model other', b'!true', b'\x1b[A', b'\t', b'bad\n', b'\xc3\xa9'])
async def test_known_codec_refusal_does_not_reserve_poison_or_prevent_supported_input(stream_case, data):
    case = stream_case
    grant = (await claim(case))['result']
    rejected = await call(case.client, 'terminal.input',
        {**frame(case, grant), 'data': base64.b64encode(data).decode()})
    assert rejected['error']['data']['code'] == 'UNSUPPORTED'
    stream = case.launch.terminal_grants.record
    assert stream.sequence == 0 and not stream.pending and stream.private_draft == ''
    assert not case.host.writes and not case.launch.peer.closed
    assert 'result' in await call(case.client, 'terminal.input', frame(case, grant))
    assert case.host.writes == [(1, b'artificial bytes')]


async def test_only_explicit_claim_mints_stream_and_native_delivery_is_not_processing_submitted_enter(stream_case):
    case = stream_case
    assert case.launch.terminal_grants.record is None
    grant = (await claim(case))['result']
    assert grant['receipt']['state'] == 'accepted'
    assert grant['inputEpoch'] == 2 and len(grant['inputGrant']) == 64
    assert grant['inputGrant'] not in repr(case.host.claims[0])
    reply = (await call(case.client, 'terminal.input', frame(case, grant)))['result']
    assert reply['watermark'] == {'epoch': 2, 'admitted': 1, 'processed': 0}
    assert case.host.writes == [(1, b'artificial bytes')]
    assert (await call(case.client, 'session.snapshot', {}))['result']['terminalMediated'] is False
    # A delivered unsubmitted draft may be retained. A submitted Enter with no
    # conclusive processing acknowledgement still blocks take-over.
    assert 'result' in await call(case.client, 'terminal.input',
        {**frame(case, grant, 2), 'data': base64.b64encode(b'\r').decode()})
    assert case.launch.terminal_grants.record.entered_sequence == 2
    params = {'expectedRunId': str(case.launch.run_id), 'attachmentId': case.attachment,
              'expectedRevision': 2, 'view': 'chat', 'reason': 'Artificial premature transfer'}
    operation = (await register(case.client, case, params, 'no-drain'))['result']['operationId']
    result = await call(case.client, 'writer.take', {**params, 'operationId': operation})
    assert result['error']['data']['code'] == 'INPUT_CONFLICT'
    assert case.broker_state.writers.gate.epoch == 2


async def test_security_s2_cancel_before_frame_reservation_does_not_poison(stream_case, monkeypatch):
    from unittest.mock import AsyncMock
    case = stream_case
    grant = (await claim(case))['result']
    boundary = case.launch.terminal_grants
    def cancelled(*args):
        raise asyncio.CancelledError()
    monkeypatch.setattr(boundary, 'frame', cancelled)
    poison = AsyncMock()
    monkeypatch.setattr(boundary, 'poison', poison)
    with pytest.raises(asyncio.CancelledError):
        await boundary.input(None, frame(case, grant), b'x')
    poison.assert_not_awaited()
    assert boundary.record is not None and boundary.record.sequence == 0


async def test_security_s1_authentication_precedes_live_frame_lock(stream_case, monkeypatch):
    from fastapi import HTTPException
    case = stream_case
    grant = (await claim(case))['result']
    def denied(*args):
        from acc.server.db import WRITE_INTENT
        assert WRITE_INTENT.get() is False
        raise HTTPException(401, 'fixture signed out')
    monkeypatch.setattr(case.ctx, 'relay_principal', denied)
    async with case.launch.terminal_grants.frame_lock:
        response = await asyncio.wait_for(case.client.post(f'/api/sessions/{SID}/relay', json={
            'jsonrpc':'2.0', 'id':'signed-out', 'method':'ry/terminal.input',
            'params':{'sessionId':SID, **frame(case, grant)}}), 2)
    assert response.status_code == 401 and not case.host.writes


async def test_security_s1_rpc_rate_limit_is_before_dispatch(public_case, monkeypatch):  # noqa: F811
    import acc.server.api.session_relay as module
    monkeypatch.setattr(module, 'RPC_RATE_LIMIT', 2, raising=False)
    monkeypatch.setattr(module, 'RPC_RATE_WINDOW', 60, raising=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(public_case.app),base_url='http://fixture') as client:
        body={'jsonrpc':'2.0','id':'rate','method':'ry/initialize','params':{'major':1,'minor':0,'features':[],'clientId':'rate'}}
        assert (await client.post(f'/api/sessions/{SID}/relay',json=body)).status_code == 200
        assert (await client.post(f'/api/sessions/{SID}/relay',json=body)).status_code == 200
        assert (await client.post(f'/api/sessions/{SID}/relay',json=body)).status_code == 429


async def test_security_s2_cancel_joined_reservation_still_poisons(stream_case, monkeypatch):
    import threading
    from unittest.mock import AsyncMock
    case = stream_case
    grant = (await claim(case))['result']
    boundary = case.launch.terminal_grants
    reached, release = threading.Event(), threading.Event()
    def reserve(*args):
        reached.set()
        assert release.wait(5)
    monkeypatch.setattr(boundary, 'frame', reserve)
    poison = AsyncMock()
    monkeypatch.setattr(boundary, 'poison', poison)
    task = asyncio.create_task(boundary.input(None, frame(case, grant), b'x'))
    try:
        assert await asyncio.to_thread(reached.wait, 5)
        task.cancel()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    poison.assert_awaited_once()


def test_security_s1_rate_limits_are_actor_session_scoped_and_bounded(monkeypatch):
    import acc.server.api.session_relay as module
    monkeypatch.setattr(module, 'RPC_RATE_LIMIT', 2)
    now = [1.0]
    limiter = module.RpcRateLimit(clock=lambda: now[0], capacity=3)
    alice = p.Actor(p.ActorKind.PERSON, 'alice', p.Channel.CHAT)
    bob = p.Actor(p.ActorKind.PERSON, 'bob', p.Channel.CHAT)
    assert limiter.admit('one', alice) and limiter.admit('one', alice)
    assert not limiter.admit('one', alice)
    assert limiter.admit('one', bob) and limiter.admit('two', alice)
    assert not limiter.admit('three', alice)  # full table cannot evict a live limit
    now[0] += 2
    assert limiter.admit('three', alice) and limiter.admit('one', alice)
