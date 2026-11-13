"""Actual main/broker native journal/settlement, with fixture Host/transcript."""
import base64

import pytest
from test_session_relay_claude_terminal_grants import claim, claude_stream  # noqa: F401
from test_session_relay_ownership import SID, ownership_case, select_owner  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.broker.claude_native_input import NativeInput
from acc.server.db import write_in_thread
from acc.server.models import RelayOperation, RelayOwnership, RelaySession
from acc.session_relay import protocol as p


async def prepare(c):
    control = await claim(c)
    stream = c.host.claims[0]
    await c.manager.input_terminal(c.attached, SID, {'inputGrant': control.input_grant, 'inputEpoch': control.input_epoch,
        'frameSeq': 1, 'data': base64.b64encode(b'Native fixture input\r').decode()})
    native = NativeInput(c.broker_state)
    operation = await native.register(stream, 1, 'Native fixture input')
    return stream, native, operation


async def test_native_claim_settles_original_receipt_without_sdk_dispatch(claude_stream):  # noqa: F811
    c = claude_stream
    stream, native, op = await prepare(c)
    assert c.broker_state.journal.lookup(op.operation_id) == ('uncertain', None)
    with c.db() as db:
        assert db.get(RelayOperation, op.operation_id).receipt['state'] == 'submitting'
        assert db.get(RelaySession, SID).active_operation_id == op.operation_id
    evidence = p.Evidence(p.EvidenceKind.PROVIDER_ADMISSION, op.operation_id, op.run_id,
                         'fixture-conversation:user:provider-message')
    receipt = await native.settle(evidence)
    assert receipt.state is p.ReceiptState.ACCEPTED and receipt.actor == stream.actor
    assert c.broker_state.journal.lookup(op.operation_id)[0] == 'accepted'
    with c.db() as db:
        assert db.get(RelaySession, SID).active_operation_id == op.operation_id  # no premature idle
    await native.processed(stream, 1, op.operation_id)
    with c.db() as db:
        assert db.get(RelaySession, SID).active_operation_id is None
    assert c.launch.terminal_grants.record.processed == 1
    assert op.operation_id not in c.launch.turn_origins
    assert len(c.owned_factory.owners) == 2  # SDK then existing TUI only


async def test_pending_native_registration_is_not_dispatched_by_sdk_queue_drain(claude_stream):  # noqa: F811
    c = claude_stream
    control = await claim(c)
    await c.manager.input_terminal(c.attached, SID, {'inputGrant': control.input_grant, 'inputEpoch': control.input_epoch,
        'frameSeq': 1, 'data': base64.b64encode(b'Native fixture input\r').decode()})
    value = await c.broker_state.peer.call('native.claude.register', {'grant': control.input_grant,
        'sequence': 1, 'text': 'Native fixture input', 'revision': control.input_epoch})
    entry = c.launch.terminal_grants.native_operations[value['grant']]
    await c.manager.drain(c.launch)
    with c.db() as db:
        assert db.get(RelayOperation, entry['operation'].operation_id).receipt['state'] == 'queued'
    assert len(c.owned_factory.owners) == 2


async def test_late_native_evidence_settles_after_revocation_but_cannot_ack_input(claude_stream):  # noqa: F811
    c = claude_stream
    stream, native, op = await prepare(c)
    c.state['revoked'] = True
    receipt = await native.settle(p.Evidence(p.EvidenceKind.PROVIDER_ADMISSION, op.operation_id, op.run_id,
                                           'fixture-conversation:user:provider-message'))
    assert receipt.state is p.ReceiptState.ACCEPTED and receipt.actor == stream.actor
    with pytest.raises(ConnectionError):
        await native.processed(stream, 1, op.operation_id)
    assert c.launch.terminal_grants.record.processed == 0


async def test_only_native_enter_can_register_and_duplicate_cannot_renew_permission(claude_stream):  # noqa: F811
    c = claude_stream
    stream, native, op = await prepare(c)
    with pytest.raises(ConnectionError):
        await c.broker_state.peer.call('native.claude.register', {'grant': stream.grant,
            'sequence': 1, 'text': 'Native fixture input', 'revision': stream.epoch})
    with pytest.raises(p.RelayError) as error:
        await c.manager.input_terminal(c.attached, SID, {'inputGrant': stream.grant, 'inputEpoch': stream.epoch,
            'frameSeq': 2, 'data': base64.b64encode(b'Cannot queue another input').decode()})
    assert error.value.code is p.ErrorCode.BUSY


async def test_refused_busy_handover_preserves_original_stream_authority(claude_stream):  # noqa: F811
    c = claude_stream
    control = await claim(c)
    c.owned_factory.current.background.add('fixture-background')
    with c.db() as db:
        previous = dict(db.get(RelayOwnership, SID).authority)
    with pytest.raises(ConnectionError):
        await select_owner(c, 'chat', 'busy-no-authority-transfer')
    with c.db() as db:
        assert db.get(RelayOwnership, SID).authority == previous
    assert await write_in_thread(lambda: c.launch.terminal_grants.authorize(control.input_grant, 'held'))


async def test_current_stream_cookie_not_original_view_cookie_authorizes_native_hooks(claude_stream):  # noqa: F811
    import time

    from fastapi import HTTPException
    c = claude_stream
    await claim(c)
    def original_cookie_expired():
        raise HTTPException(401, 'artificial original view cookie expired')
    c.launch.resolve = original_cookie_expired
    assert await c.broker_state.owner_authorized() is True
    c.launch.terminal_grants.record.expires = time.monotonic() - 1
    with pytest.raises(ConnectionError):
        await c.broker_state.owner_authorized()  # never fall back to another cookie/owner grant
