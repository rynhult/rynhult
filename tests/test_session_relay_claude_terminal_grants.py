"""Production L1/main/broker grant path, explicitly artificial Host qualification."""
import base64

import pytest
from test_session_relay_ownership import SID, ownership_case, select_owner  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.server.db import write_intent
from acc.server.models import RelaySession
from acc.server.session_relay.hub import Hub
from acc.session_relay import protocol as p
from acc.session_relay.adapters.base import TerminalRun, Watermark, WriteResult


class Attached:
    def __init__(self, resolve, attachment='fixture-terminal-tab'):
        self.resolve, self.attachment_id = resolve, attachment
    def __call__(self):
        return self.resolve()


@pytest.fixture
async def claude_stream(ownership_case, monkeypatch):  # noqa: F811
    c = ownership_case
    await select_owner(c, 'terminal', 'person-select-terminal')
    state = next(iter(c.factory.active.values()))
    current = c.owned_factory.current
    # These tests do not qualify native process placement or draft state.
    monkeypatch.setattr(current.process, 'current', lambda: current.process.returncode is None, raising=False)
    original = c.owned_factory.authorize
    async def authorize(spec=None, operation=None, *, lifecycle=False):
        return await original(spec, operation)
    monkeypatch.setattr(c.owned_factory, 'authorize', authorize)
    class Host:
        def __init__(self):
            self.run = TerminalRun(SID, str(c.launch.run_id))
            self.claims, self.writes = [], []
        async def claim_input(self, grant):
            assert await state.terminal_value(grant.grant, 'held') == grant
            self.claims.append(grant)
        async def write_frames(self, grant, sequence, data):
            self.writes.append((sequence, data))
            return WriteResult(Watermark(grant.epoch, sequence, 0))
    host = Host()
    current.host = state.terminal_host = host
    c.host, c.broker_state, c.attached = host, state, Attached(c.resolve)
    @write_intent
    def qualify():
        with c.db() as db:
            db.get(RelaySession, SID).capabilities = {'text': 'qualified', 'terminal.input': 'qualified'}
            db.commit()
    c.qualify = qualify
    c.qualify()
    monkeypatch.setattr('acc.server.session_relay.writers.PUBLIC_WRITER_QUALIFIED', True)
    return c


async def claim(c, key='person-claim', resolve=None):
    resolve = resolve or c.attached
    with c.db() as db:
        revision = db.get(RelaySession, SID).control_revision
    params = {'sessionId': SID, 'expectedRunId': str(c.launch.run_id),
              'attachmentId': resolve.attachment_id, 'expectedRevision': revision}
    registered = Hub(c.ctx).register(resolve, SID, str(c.launch.run_id), key,
                                   'control.claim', p.operation_digest('control.claim', params))
    return await c.manager.claim_terminal(resolve, SID, {**params, 'operationId': registered.operation_id})


async def test_l1_explicit_claim_binds_current_run_actor_attachment_and_epoch(claude_stream):
    c = claude_stream
    assert c.launch.terminal_grants.record is None and not c.host.claims
    grant = await claim(c)
    assert grant.receipt.state is p.ReceiptState.ACCEPTED
    value = c.host.claims[0]
    assert value.actor == c.resolve()[1] and value.attachment_id == c.attached.attachment_id
    assert value.run.run_id == str(c.launch.run_id)
    assert grant.input_epoch == value.epoch == 2
    assert value.grant not in repr(value)
    params = {'inputGrant': grant.input_grant, 'inputEpoch': grant.input_epoch, 'frameSeq': 1,
              'data': base64.b64encode(b'artificial bytes').decode()}
    result = await c.manager.input_terminal(c.attached, SID, params)
    assert result['watermark'] == {'epoch': 2, 'admitted': 1, 'processed': 0}
    assert c.host.writes == [(1, b'artificial bytes')]
    with pytest.raises(p.RelayError) as error:
        await select_owner(c, 'chat', 'no-unprocessed-handover')
    assert error.value.code is p.ErrorCode.INPUT_CONFLICT


async def test_l1_other_attachment_and_old_epoch_cannot_send(claude_stream):
    c = claude_stream
    grant = await claim(c)
    params = {'inputGrant': grant.input_grant, 'inputEpoch': grant.input_epoch, 'frameSeq': 1,
              'data': base64.b64encode(b'artificial bytes').decode()}
    for resolve, changes in ((Attached(c.resolve, 'other-tab'), {}), (c.attached, {'inputEpoch': 1}),
                             (c.attached, {'inputGrant': 'wrong'})):
        with pytest.raises(p.RelayError):
            await c.manager.input_terminal(resolve, SID, {**params, **changes})
    assert not c.host.writes


async def test_l1_feature_gate_off_cannot_mint_even_with_fixture_capabilities(claude_stream, monkeypatch):
    c = claude_stream
    monkeypatch.setattr('acc.server.session_relay.writers.PUBLIC_WRITER_QUALIFIED', False)
    with pytest.raises(p.RelayError):
        await claim(c)
    assert c.launch.terminal_grants.record is None and not c.host.claims


async def test_l1_claim_does_not_enable_native_sdk_redispatch(claude_stream):
    c = claude_stream
    grant = await claim(c)
    before = len(c.owned_factory.owners)
    with pytest.raises(p.RelayError) as error:
        await c.launch.terminal_grants.native_submit(grant.input_grant, 1, 'artificial', grant.input_epoch)
    assert error.value.code is p.ErrorCode.UNSUPPORTED
    assert len(c.owned_factory.owners) == before


async def test_l1_authority_revoked_during_process_check_never_reaches_host(claude_stream, monkeypatch):
    c = claude_stream
    def revoke():
        c.state['revoked'] = True
        return True
    monkeypatch.setattr(c.owned_factory.current.process, 'current', revoke)
    with pytest.raises(p.RelayError):
        await claim(c)
    assert not c.host.claims and not c.host.writes
    assert c.launch.terminal_grants.record is None


async def test_l1_expired_grant_after_process_await_never_reaches_host(claude_stream, monkeypatch):
    import time
    c = claude_stream
    def expire():
        c.launch.terminal_grants.record.expires = time.monotonic() - 1
        return True
    monkeypatch.setattr(c.owned_factory.current.process, 'current', expire)
    with pytest.raises(p.RelayError):
        await claim(c)
    assert not c.host.claims and not c.host.writes
    assert c.launch.terminal_grants.record is None


async def test_l1_holder_drain_is_an_observation_not_a_claim_or_processing_proof(claude_stream):
    c = claude_stream
    with pytest.raises(p.RelayError):
        await c.manager.drain_terminal(c.attached, SID, {
            'attachmentId': c.attached.attachment_id, 'inputGrant': 'missing'})
    assert not c.host.claims
    grant = await claim(c)
    record = c.launch.terminal_grants.record
    expires = record.expires
    params = {'attachmentId': c.attached.attachment_id, 'inputGrant': grant.input_grant}
    assert await c.manager.drain_terminal(c.attached, SID, params) == {'epoch': 2, 'admitted': 0, 'processed': 0}
    await c.manager.input_terminal(c.attached, SID, {**params, 'inputEpoch': 2, 'frameSeq': 1,
        'data': base64.b64encode(b'artificial bytes').decode()})
    assert await c.manager.drain_terminal(c.attached, SID, params) == {'epoch': 2, 'admitted': 1, 'processed': 0}
    with pytest.raises(p.RelayError):
        await c.manager.drain_terminal(Attached(c.resolve, 'another-tab'), SID, {
            **params, 'attachmentId': 'another-tab'})
    assert record.expires == expires and len(c.host.claims) == len(c.host.writes) == 1


@pytest.mark.parametrize('data',[b'/config\r',b'  !shell\r',b'\x1b[200~paste\x1b[201~',b'\x03',b'\xc3\xa9',b'a\rb'])
async def test_known_unsupported_frame_refuses_before_main_sequence_or_host(claude_stream,data):
    c=claude_stream
    grant=await claim(c)
    params={'inputGrant':grant.input_grant,'inputEpoch':grant.input_epoch,'frameSeq':1,
        'data':base64.b64encode(data).decode()}
    with pytest.raises(p.RelayError) as error:await c.manager.input_terminal(c.attached,SID,params)
    assert error.value.code is p.ErrorCode.UNSUPPORTED
    stream=c.launch.terminal_grants.record
    assert stream.phase=='active' and stream.sequence==stream.processed==0 and not stream.pending
    assert not c.host.writes and not c.launch.peer.closed
    await c.manager.input_terminal(c.attached,SID,{**params,'data':base64.b64encode(b'Valid continuation').decode()})
    assert c.host.writes==[(1,b'Valid continuation')]
