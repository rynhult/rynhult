"""Joint production main/broker/Host/hooks and real offline native PTY proof.

Owner launch and authenticated caller are fixture setup; no browser qualification.
"""
import asyncio
import base64
import importlib.util
import json
import os
import shutil
from pathlib import Path

import pytest
from test_session_relay_claude_terminal_grants import claim, claude_stream  # noqa: F401
from test_session_relay_ownership import SID, ownership_case  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.broker.claude_admission import NativeAdmission
from acc.broker.claude_hooks import ClaudeHookServer
from acc.broker.claude_native_input import NativeInput
from acc.broker.relay_enclosure import incarnation
from acc.broker.relay_process_tree import descends
from acc.server.models import RelayOperation, RelaySession
from acc.session_relay import protocol as p
from acc.session_relay.claude_input_host import ClaudeInputHost

NATIVE = '11111111-1111-4111-8111-111111111111'
TEXT = b'Native TUI isolated fixture turn'


pytestmark = pytest.mark.relay_native


@pytest.mark.parametrize('mode', ['accepted', 'deny_prompt', 'lost_stop'])
@pytest.mark.parametrize('ownership_case', [NATIVE], indirect=True)
async def test_real_native_production_grants_host_hook_and_settlement(claude_stream, record_property, mode):  # noqa: F811
    if not shutil.which('bwrap') or not Path('/usr/bin/claude').exists():
        pytest.skip('Pinned installed CLI and enforced namespace required; no fallback')
    c = claude_stream
    path = Path(__file__).resolve().parents[1] / 'scripts/session_relay_qualification/probe.py'
    spec = importlib.util.spec_from_file_location('claude_joint_probe', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    loop = asyncio.get_running_loop()
    host = server = hooks = None
    events, failures, observation_failures = [], [], []
    native = NativeInput(c.broker_state)
    async def setup(output):
        nonlocal server, hooks
        (output / 'joint-mode').write_text(mode)
        await c.owned_factory.hooks.close()
        hooks = ClaudeHookServer(output / 'hook', c.broker_state.spec, c.owned_factory.authorize,
            c.broker_state.publish, observe, accept_pid=lambda pid:descends(pid,os.getpid(),incarnation(os.getpid())))
        c.owned_factory.hooks = hooks
        await hooks.start()
        async def control(reader, writer):
            nonlocal host
            try:
                request = json.loads(await asyncio.wait_for(reader.readline(), 5))
                if request == {'method': 'ready'}:
                    root = output / 'native-projects'
                    paths = list(root.glob('*/' + NATIVE + '.jsonl'))
                    assert len(paths) == 1
                    admission = NativeAdmission(root, paths[0], NATIVE)
                    async def open_input(run):
                        return await asyncio.open_unix_connection(str(output / 'input.sock'))
                    async def validate(grant):
                        return await c.broker_state.terminal_value(grant.grant, 'held') == grant
                    host = ClaudeInputHost(c.host.run, open_input, validate, observer=c.host,
                        native_id=NATIVE, reader=admission, register=native.register, settle=native.settle,
                        processed=native.processed, authorize_boundary=lambda: c.owned_factory.authorize())
                    c.owned_factory.current.host = c.broker_state.terminal_host = host
                    c.owned_factory.current.boundary = host.freeze_boundary
                    grant = await claim(c)
                    async def send(sequence, data):
                        return await c.manager.input_terminal(c.attached, SID, {'inputGrant': grant.input_grant,
                            'inputEpoch': grant.input_epoch, 'frameSeq': sequence,
                            'data': base64.b64encode(data).decode()})
                    await send(1, TEXT)
                    with pytest.raises(p.RelayError):
                        await host.freeze_boundary(host.grant)
                    assert host.processed == 0
                    await asyncio.sleep(.7)
                    try:
                        await send(2, b'\r')
                    except p.RelayError:
                        # Revocation can win the post-IO held check after the
                        # exact frame reached the native hook. Never replay it.
                        assert mode == 'deny_prompt' and c.state['revoked']
                        assert 'UserPromptSubmit' in events and host.admitted == 2
                    reply = {'ready': True}
                elif request == {'method': 'status'}:
                    reply = {'processed': host.processed, 'observationFailures': list(observation_failures)}
                elif request == {'method': 'detach'}:
                    if mode == 'accepted':
                        assert host.boundary.verified_empty and host.processed == 2
                    elif mode == 'deny_prompt':
                        assert host.processed == 0 and host.boundary.partial and native.pending is None
                        assert host.boundary.draft == TEXT.decode()
                    else:
                        assert host.processed == 0 and not host.boundary.verified_empty
                        op = native.pending['operation']
                        assert c.broker_state.journal.lookup(op.operation_id) == ('uncertain', None)
                        with c.db() as db:
                            assert db.get(RelayOperation, op.operation_id).receipt['state'] == 'submitting'
                    await host.close()
                    await hooks.close()
                    c.owned_factory.hooks = None
                    reply = {'detached': True}
                else:
                    raise ValueError('Unexpected fixture control method')
            except Exception as error:
                failures.append(repr(error))
                reply = {'error': repr(error)}
            writer.write(json.dumps(reply).encode() + b'\n')
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        server = await asyncio.start_unix_server(control, str(output / 'joint.sock'))
    async def observe(payload):
        events.append(payload['hook_event_name'])
        if mode == 'deny_prompt' and payload['hook_event_name'] == 'UserPromptSubmit':
            c.state['revoked'] = True
        try:
            await c.broker_state.observe(payload)
        except Exception as error:
            observation_failures.append(payload['hook_event_name'] + ':' + type(error).__name__ + ':' + str(error))
            raise
    async def cleanup():
        if c.owned_factory.hooks is hooks and hooks is not None:
            await hooks.close()
            c.owned_factory.hooks = None
    def prepare(output):
        asyncio.run_coroutine_threadsafe(setup(output), loop).result(timeout=10)
        return lambda: asyncio.run_coroutine_threadsafe(cleanup(), loop).result(timeout=10)
    try:
        result = await asyncio.to_thread(module.capture_claude_resume, enclosure=True, tui=True, joint=prepare)
        assert not failures
        assert result['tui']['frames'] == [TEXT.decode(), '\r']
        assert result['tui']['completed'] and result['tui']['empty_tree']
        if mode != 'deny_prompt':
            assert result['tui']['exit'] == 0
        assert events.count('UserPromptSubmit') == 1
        assert events.count('Stop') == (1 if mode == 'accepted' else 0)
        if mode == 'accepted':
            assert host.boundary.verified_empty and host.processed == host.admitted == 2
            assert native.pending is None and c.broker_state.active_operation is None
        else:
            assert host.processed == 0 and not host.boundary.verified_empty
        assert result['tui']['native_model_requests'] == (0 if mode == 'deny_prompt' else 1)
        with c.db() as db:
            rows = [row for row in db.query(RelayOperation).all() if row.receipt['method'] == 'turn.submit']
            assert len(rows) == (0 if mode == 'deny_prompt' else 1)
            if rows:
                receipt = rows[0].receipt
                assert receipt['state'] == 'accepted'
                assert receipt['actor']['principal'] == c.resolve()[1].principal
                assert c.broker_state.journal.lookup(rows[0].id)[0] == 'accepted'
            if mode == 'accepted':
                assert db.get(RelaySession, SID).active_operation_id is None
            elif mode == 'lost_stop':
                assert db.get(RelaySession, SID).active_operation_id == rows[0].id
        assert result['tui']['terminalMediated'] is host.attested is False
        assert all(stage['empty_tree'] for stage in result['results'])
        record_property('joint_native_scope', result['tui']['scope'])
        record_property('native_cli_digest', result['cli_digest'])
    finally:
        if server:
            server.close()
            await server.wait_closed()
        if host:
            await host.close()
