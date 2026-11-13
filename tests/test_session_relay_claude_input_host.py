"""Production Host/main/journal composition with artificial native transcript/PTY."""
import asyncio
import base64
import json

import pytest
from test_session_relay_claude_terminal_grants import claim, claude_stream  # noqa: F401
from test_session_relay_ownership import SID, ownership_case, select_owner  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.broker.claude_admission import NativeAdmission
from acc.broker.claude_native_input import NativeInput
from acc.server.models import RelayOperation, RelaySession
from acc.session_relay import protocol as p
from acc.session_relay.claude_input_host import ClaudeInputHost


@pytest.fixture
async def native_host(claude_stream, tmp_path):  # noqa: F811
    c = claude_stream
    root = tmp_path / 'native-projects'
    project = root / 'workspace'
    project.mkdir(parents=True, mode=0o700)
    path = project / 'fixture-conversation.jsonl'
    path.touch(mode=0o600)
    reader = NativeAdmission(root, path, 'fixture-conversation')
    native = NativeInput(c.broker_state)
    writes = []
    errors = []
    original_handler = c.broker_state.peer.handle
    async def diagnostic(method, params):
        import traceback
        try:
            return await original_handler(method, params)
        except Exception:
            errors.append(traceback.format_exc())
            raise
    c.broker_state.peer.handle = diagnostic
    c.native_errors = errors
    class Writer:
        def write(self, data):
            writes.append(data)
        async def drain(self):
            pass
        def close(self):
            pass
        async def wait_closed(self):
            pass
    async def open_input(run):
        return asyncio.StreamReader(), Writer()
    async def validate(grant):
        try:
            return await c.broker_state.terminal_value(grant.grant, 'held') == grant
        except (ConnectionError, p.RelayError):
            return False
    host = ClaudeInputHost(c.host.run, open_input, validate, observer=c.host,
        native_id='fixture-conversation', reader=reader, register=native.register,
        settle=native.settle, processed=native.processed,
        authorize_boundary=lambda: c.owned_factory.authorize())
    owner = c.owned_factory.current
    owner.host = c.broker_state.terminal_host = host
    owner.boundary = host.freeze_boundary
    c.native_host, c.native_path, c.native_writes, c.native_controller = host, path, writes, native
    try:
        yield c
    finally:
        await host.close()


def transcript(c):
    rows = [{'type': 'user', 'uuid': 'native-user', 'parentUuid': None,
             'message': {'role': 'user', 'content': 'Native fixture input'}},
            {'type': 'assistant', 'uuid': 'native-assistant', 'parentUuid': 'native-user',
             'message': {'role': 'assistant', 'id': 'actual-fixture-provider-id', 'stop_reason': 'end_turn'}}]
    with c.native_path.open('a') as file:
        for row in rows:
            file.write(json.dumps({**row, 'sessionId': 'fixture-conversation', 'isSidechain': False}) + '\n')


async def enter(c):
    control = await claim(c)
    await c.manager.input_terminal(c.attached, SID, {'inputGrant': control.input_grant,
        'inputEpoch': control.input_epoch, 'frameSeq': 1, 'data': base64.b64encode(b'Native fixture input\r').decode()})
    await c.broker_state.observe({'session_id': 'fixture-conversation', 'hook_event_name': 'UserPromptSubmit',
                                 'prompt': 'Native fixture input'})
    return control


async def test_native_host_consumes_exact_frame_settles_and_hands_same_id_back(native_host):
    c = native_host
    control = await enter(c)
    operation = c.native_controller.pending['operation']
    assert c.native_host.processed == 0 and c.native_host.boundary.ticket is not None
    transcript(c)
    await c.broker_state.observe({'session_id': 'fixture-conversation', 'hook_event_name': 'Stop'})
    assert c.native_host.processed == 1 and c.native_host.boundary.verified_empty
    with c.db() as db:
        assert db.get(RelayOperation, operation.operation_id).receipt['state'] == 'accepted'
        assert db.get(RelaySession, SID).active_operation_id is None
    try:
        await select_owner(c, 'chat', 'processed-native-handover')
    except Exception as error:
        raise AssertionError('Fixture native switch failed: ' + '\n'.join(c.native_errors)) from error
    assert c.owned_factory.current.spec.binding.native_conversation == 'fixture-conversation'
    assert c.native_host.closed and c.native_host.boundary.frozen
    with pytest.raises(p.RelayError):
        await c.native_host.write_frames(c.native_host.grant, 2, b'old writer')
    assert control.input_epoch == 2 and len(c.owned_factory.owners) == 3
    assert len(c.native_writes) == 1


async def test_native_stop_without_provider_chain_cannot_advance_or_handover(native_host):
    c = native_host
    await enter(c)
    op = c.native_controller.pending['operation']
    with pytest.raises(p.RelayError):
        await c.broker_state.observe({'session_id': 'fixture-conversation', 'hook_event_name': 'Stop'})
    assert c.native_host.processed == 0 and c.native_host.boundary.partial
    with c.db() as db:
        assert db.get(RelayOperation, op.operation_id).receipt['state'] == 'submitting'
    with pytest.raises(p.RelayError):
        await select_owner(c, 'chat', 'unknown-is-not-idle')


async def test_late_native_acceptance_does_not_restore_revoked_input(native_host):
    c = native_host
    await enter(c)
    op = c.native_controller.pending['operation']
    transcript(c)
    c.state['revoked'] = True
    with pytest.raises(p.RelayError):
        await c.native_host.observe_native({'session_id': 'fixture-conversation', 'hook_event_name': 'Stop'})
    with c.db() as db:
        assert db.get(RelayOperation, op.operation_id).receipt['state'] == 'accepted'
    assert c.native_host.processed == 0 and c.native_host.boundary.partial
    assert c.native_host.attested is False


async def test_native_session_end_refuses_later_input_before_process_reap(native_host):
    c = native_host
    control = await enter(c)
    transcript(c)
    await c.broker_state.observe({'session_id': 'fixture-conversation', 'hook_event_name': 'Stop'})
    await c.broker_state.observe({'session_id': 'fixture-conversation', 'hook_event_name': 'SessionEnd'})
    assert c.native_host.ending
    with pytest.raises(p.RelayError):
        await c.manager.input_terminal(c.attached, SID, {'inputGrant': control.input_grant,
            'inputEpoch': control.input_epoch, 'frameSeq': 2, 'data': base64.b64encode(b'late input').decode()})
    assert len(c.native_writes) == 1


async def test_native_modal_blocks_new_frames_but_can_settle_original_turn(native_host):
    c=native_host;await enter(c)
    operation=c.native_controller.pending['operation']
    c.native_host.native_prompt_active=True
    before=list(c.native_writes)
    with pytest.raises(p.RelayError):
        await c.native_host.write_frames(c.native_host.grant,2,b'\r')
    assert c.native_writes==before
    transcript(c)
    await c.broker_state.observe({'session_id':'fixture-conversation','hook_event_name':'Stop'})
    assert c.native_host.processed==1
    with c.db() as db:assert db.get(RelayOperation,operation.operation_id).receipt['state']=='accepted'
