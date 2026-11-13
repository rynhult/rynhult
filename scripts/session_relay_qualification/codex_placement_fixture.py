"""Actual production Codex broker/owned view; fixed offline Responses fixture."""
import asyncio
import errno
import json
import os
import select
import signal
import socket
import sqlite3
import time
from dataclasses import replace
from pathlib import Path

from acc.broker import ops
from acc.broker.config import BrokerConfig
from acc.broker.relay_runtime import CLI_DIGEST, RuntimeTransportFactory
from acc.broker.server import Broker
from acc.session_relay import protocol as p


async def controls(state, requests):
    from websockets.asyncio.client import unix_connect
    native = state.binding.native_conversation
    controls = [
        ('config/value/write',{'keyPath':'model','value':'other'}),
        ('config/batchWrite',{'edits':[]}),('account/login/start',{'type':'apiKey','apiKey':'artificial'}),
        ('account/logout',{}),('thread/start',{}),('thread/fork',{'threadId':native}),
        ('turn/steer',{'threadId':native,'input':[{'type':'text','text':'not delivered'}]}),
        ('command/exec',{'command':['true']}),('thread/rollback',{'threadId':native,'numTurns':1}),
        ('thread/resume',{'threadId':native,'config':{'features.hooks':False}}),
        ('thread/resume',{'threadId':native,'approvalPolicy':'never'}),
        ('thread/resume',{'threadId':native,'model':'other'}),
        ('turn/start',{'threadId':native,'model':'other','input':[{'type':'text','text':'not delivered'}]}),
        ('turn/start',{'threadId':native,'effort':'high','input':[{'type':'text','text':'not delivered'}]}),
        ('turn/start',{'threadId':native,'input':[{'type':'localImage','path':'/tmp/attachment.png'}]}),
        ('turn/start',{'threadId':native,'input':[{'type':'image','url':'https://example.com/fixture.png'}]}),
        ('turn/start',{'threadId':native,'input':[{'type':'text','text':'not delivered','text_elements':[{}]}]}),
        ('session/resume',{'sessionId':native}),('session/stop',{}),('editor/open',{'path':'fixture'}),
    ]
    before = (len(requests), state.owner.attestation.process_identity,
        state.terminal_host.admitted, state.terminal_host.processed)
    async with unix_connect(state.native_socket.connect_path, compression=None) as client:
        await client.send(json.dumps({'id': 0, 'method': 'initialize',
            'params': {'clientInfo': {'name': 'codex-tui', 'version': '0.167.1'}}}))
        assert 'result' in json.loads(await client.recv())
        for identifier, (method, params) in enumerate(controls, 1):
            await client.send(json.dumps({'id': identifier, 'method': method, 'params': params}))
            response = json.loads(await client.recv())
            assert response.get('id') == identifier and 'error' in response, response
            assert before == (len(requests), state.owner.attestation.process_identity,
                state.terminal_host.admitted, state.terminal_host.processed)
    return {'refused': len(controls), 'noEffects': True}


async def main():
    assert {name for _, name in socket.if_nameindex()} == {'lo'}
    with socket.socket() as check:
        assert check.connect_ex(('192.0.2.1', 443)) == errno.ENETUNREACH
    data = json.loads(Path('/evidence/config.json').read_text())
    requests = []
    timings = []
    def span(metric, method, started, outcome):
        if len(timings) < 512:
            timings.append({'metric': metric, 'method': method, 'ms': (time.perf_counter() - started) * 1000, 'outcome': outcome})
    permission_once = False
    model_release = asyncio.Event()
    model_release.set()
    model_waiting = asyncio.Event()
    connect_release = asyncio.Event()
    connect_waiting = asyncio.Event()
    connect_written = [0]
    message_release, message_waiting = asyncio.Event(), asyncio.Event()
    message_release.set()
    async def model(reader, writer):
        nonlocal permission_once
        try:
            header = await reader.readuntil(b'\r\n\r\n')
            size = next(int(line.split(b':', 1)[1]) for line in header.split(b'\r\n') if line.lower().startswith(b'content-length:'))
            assert 0 < size <= 1 << 20
            payload = json.loads(await reader.readexactly(size))
            requests.append({'input_markers': [marker for marker in ('First production Chat', 'Production Terminal input', 'Chat after production take', 'Retain this unsent native draft', 'Fresh replacement Terminal input')
                if marker in json.dumps(payload.get('input', []))]})
            model_waiting.set()
            await asyncio.wait_for(model_release.wait(), 30)
            model_waiting.clear()
            count = len(requests)
            message = {'type': 'message', 'id': 'msg_' + str(count), 'role': 'assistant', 'status': 'completed',
                'content': [{'type': 'output_text', 'text': 'Production fixture response ' + str(count) + '.', 'annotations': []}]}
            response = {'id': 'resp_' + str(count), 'object': 'response', 'created_at': 1, 'status': 'completed',
                'model': 'fixture-model', 'output': [message], 'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}
            events = [{'type': 'response.created', 'response': {**response, 'status': 'in_progress', 'output': []}},
                {'type': 'response.output_item.added', 'output_index': 0, 'item': {**message, 'status': 'in_progress', 'content': []}},
                {'type': 'response.content_part.added', 'output_index': 0, 'content_index': 0, 'item_id': message['id'],
                    'part': {**message['content'][0], 'text': ''}},
                {'type': 'response.output_text.delta', 'output_index': 0, 'content_index': 0, 'item_id': message['id'], 'delta': message['content'][0]['text']},
                {'type': 'response.output_text.done', 'output_index': 0, 'content_index': 0, 'item_id': message['id'], 'text': message['content'][0]['text']},
                {'type': 'response.content_part.done', 'output_index': 0, 'content_index': 0, 'item_id': message['id'], 'part': message['content'][0]},
                {'type': 'response.output_item.done', 'output_index': 0, 'item': message}, {'type': 'response.completed', 'response': response}]
            if permission_once:
                permission_once = False
                call = {'id': 'fc_permission_fixture', 'type': 'function_call', 'call_id': 'call_permission_fixture',
                    'name': 'exec_command', 'arguments': json.dumps({'cmd': 'printf fixture > /home/probe/relay-permission-forbidden',
                        'sandbox_permissions': 'require_escalated', 'justification': 'Offline fixture requires a person decision'}),
                    'status': 'completed'}
                response['output'] = [call]
                events = [
                    {'type': 'response.created', 'response': {**response, 'status': 'in_progress', 'output': []}},
                    {'type': 'response.output_item.added', 'output_index': 0, 'item': {**call, 'arguments': '', 'status': 'in_progress'}},
                    {'type': 'response.function_call_arguments.delta', 'output_index': 0, 'item_id': call['id'], 'delta': call['arguments']},
                    {'type': 'response.function_call_arguments.done', 'output_index': 0, 'item_id': call['id'], 'arguments': call['arguments']},
                    {'type': 'response.output_item.done', 'output_index': 0, 'item': call},
                    {'type': 'response.completed', 'response': response},
                ]
            body = ''.join('event: ' + event['type'] + '\ndata: ' + json.dumps({**event, 'sequence_number': number}) + '\n\n' for number, event in enumerate(events)).encode()
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\nContent-Length: '
                + str(len(body)).encode() + b'\r\n\r\n' + body)
            await writer.drain()
        finally:
            writer.close()
    model_server = await asyncio.start_server(model, '127.0.0.1', 0)
    port = model_server.sockets[0].getsockname()[1]
    config = BrokerConfig(socket_path=Path('/evidence/broker.sock'), run_dir=Path(data['run_dir']),
        allowed_roots=(Path(data['workspace']),), allowed_uids=frozenset({os.getuid()}),
        providers={'codex': '/usr/bin/codex'}, tmux_socket='relay-codex-placement', hook_path='/evidence/noop-hook')
    Path('/evidence/noop-hook').write_text('#!/bin/sh\nexit 0\n')
    Path('/evidence/noop-hook').chmod(0o500)
    # Diagnostics only: preserve the real owner command/environment, hold its
    # otherwise-discarded stderr in this artificial private namespace.
    native_metadata = {'fault': None, 'changes': 0}
    stdio_fault = {}
    original_spawn = asyncio.create_subprocess_exec
    async def diagnostic_spawn(*args, **kwargs):
        if len(args) > 1 and args[1] == 'app-server':
            with Path('/evidence/native-error.log').open('a') as stream:
                stream.write('Native argv: ' + json.dumps(args) + '\n')
            with Path('/evidence/native-error.log').open('ab') as stream:
                process = await original_spawn(*args, **{**kwargs, 'stderr': stream})
            original_line = process.stdout.readline
            async def diagnostic_line():
                line = await original_line()
                if line:
                    value = json.loads(line)
                    if native_metadata['fault'] and value.get('method') == 'item/commandExecution/requestApproval':
                        fault = native_metadata['fault']
                        params = value['params']
                        if fault == 'thread':
                            params['threadId'] = 'old-native-thread'
                        elif fault == 'turn':
                            params['turnId'] = 'old-native-turn'
                        elif fault == 'item':
                            params['itemId'] = None
                        elif fault == 'environment':
                            params['environmentId'] = 'foreign-worker'
                        elif fault == 'started':
                            params['startedAtMs'] = 'untrusted-time'
                        elif fault == 'extra':
                            params['actor'] = {'principal': 'another-person'}
                        else:
                            raise AssertionError(fault)
                        line = json.dumps(value).encode() + b'\n'
                        native_metadata['changes'] += 1
                    if stdio_fault.get('id') == value.get('id') and 'method' not in value:
                        stdio_fault['responses'] += 1
                        stdio_fault['native_result'] = isinstance(value.get('result', {}).get('turn'), dict)
                        kind = stdio_fault['kind']
                        if kind in ('read-eof', 'read-partial'):
                            process.stdout._transport.close()
                            return b'' if kind == 'read-eof' else line[:len(line) // 2]
                        if kind == 'lost-response':
                            return await diagnostic_line()  # consume exactly the real matching reply, never fabricate one
                    if 'method' in value and 'id' in value:
                        with Path('/evidence/native-error.log').open('a') as stream:
                            stream.write('Native request: ' + json.dumps(value) + '\n')
                    if 'error' in value:
                        with Path('/evidence/native-error.log').open('a') as stream:
                            stream.write(json.dumps(value['error']) + '\n')
                    if isinstance(value.get('result'), dict) and 'thread' in value['result']:
                        thread = value['result']['thread']
                        with Path('/evidence/native-error.log').open('a') as stream:
                            stream.write(json.dumps({'thread': {key: thread.get(key) for key in ('id', 'historyMode', 'turns', 'path', 'ephemeral')}}) + '\n')
                return line
            process.stdout.readline = diagnostic_line
            return process
        return await original_spawn(*args, **kwargs)
    asyncio.create_subprocess_exec = diagnostic_spawn
    ops.prepare_tmux(config)  # same account-service startup before owned views
    from acc.broker.relay_tui import OwnedTUI
    original_close_view = OwnedTUI.close
    async def diagnostic_close_view(view):
        if view.started and not view.closed:
            capture = ops.tmux(config, 'capture-pane', '-p', '-J', '-S', '-100', '-t', '=' + view.name + ':')
            with Path('/evidence/preclose-screen.jsonl').open('a') as stream:
                stream.write(json.dumps({'name': view.name, 'peer': view.peer,
                    'bootstrapped': view.bootstrapped(), 'screen': capture.stdout, 'error': capture.stderr}) + '\n')
        return await original_close_view(view)
    OwnedTUI.close = diagnostic_close_view
    factory = RuntimeTransportFactory(config)
    import traceback

    from acc.broker.relay_runtime import BrokerRuntime
    original_handle = BrokerRuntime.handle
    refusal_count = 0
    async def diagnostic(self, method, params):
        nonlocal refusal_count
        started, outcome = time.perf_counter(), 'error'
        try:
            result = await original_handle(self, method, params)
            outcome = 'returned'
            if method == 'stop':
                views = [view for view, _host in self.owned_generations]
                if self.owned_view is not None and self.owned_view not in views:
                    views.append(self.owned_view)
                Path('/evidence/retired-stop.json').write_text(json.dumps({
                    'result': result, 'producer': self.owner.runtime.exit_proven,
                    'views': [view.exit_proven for view in views], 'active': len(factory.active)}))
            return result
        except BaseException as error:
            with Path('/evidence/errors.log').open('a') as stream:
                stream.write(traceback.format_exc())
            # Safe private metadata survives the probe TemporaryDirectory. Never
            # copy native prompts, params, tokens, environment or raw exceptions.
            if refusal_count < 64:
                refusal_count += 1
                record = {'boundary': method if method in ('stage', 'dispatch', 'terminal.input',
                    'terminal.claim', 'readiness', 'terminal.capture') else 'runtime_rpc',
                    'exception': type(error).__name__,
                    'code': error.code.value if isinstance(error, p.RelayError) else None,
                    'stack': [{'file': Path(frame.filename).name, 'function': frame.name, 'line': frame.lineno}
                        for frame in traceback.extract_tb(error.__traceback__)[-12:]]}
                with (Path(data['run_dir']) / 'relay-native-refusals.jsonl').open('a') as stream:
                    stream.write(json.dumps(record) + '\n')
            raise
        finally:
            if method in ('stage', 'dispatch', 'terminal.input', 'terminal.claim'):
                span('broker_rpc', method, started, outcome)
    BrokerRuntime.handle = diagnostic
    from acc.broker.session_relay import AdmissionJournal
    def timed_journal(method):
        original = getattr(AdmissionJournal, method)
        def measured(self, *args, **kwargs):
            started, outcome = time.perf_counter(), 'error'
            try:
                result = original(self, *args, **kwargs)
                outcome = 'returned'
                return result
            finally:
                span('durable_journal', method, started, outcome)
        setattr(AdmissionJournal, method, measured)
    for journal_method in ('claim', 'settle'):
        timed_journal(journal_method)
    from acc.session_relay.native_input_host import NativeInputHost
    original_frames = NativeInputHost.write_frames
    async def timed_frames(self, grant, sequence, data):
        started, outcome = time.perf_counter(), 'error'
        try:
            result = await original_frames(self, grant, sequence, data)
            outcome = 'returned'
            return result
        finally:
            span('host_frame', 'write_frames', started, outcome)
    NativeInputHost.write_frames = timed_frames
    original_read = BrokerRuntime.native_read
    async def diagnostic_read(self, method, params):
        try:
            return await original_read(self, method, params)
        except BaseException:
            with Path('/evidence/errors.log').open('a') as stream:
                stream.write(method + ': ' + traceback.format_exc())
            raise
    BrokerRuntime.native_read = diagnostic_read
    broker = Broker(config, relay_transport=factory)
    server = await asyncio.start_unix_server(broker.handle, str(config.socket_path))
    finished = asyncio.Event()
    last_state = None
    interrupt_proof = {}
    interrupt_journal_root = None
    async def inspect(reader, writer):
        try:
            nonlocal permission_once, last_state, interrupt_proof, interrupt_journal_root
            method = json.loads(await reader.readline())['method']
            state = next(iter(factory.active.values()), None)
            if state is not None:
                last_state = state
            if method == 'qualify':
                if not getattr(state, 'fixture_timed', False):
                    state.fixture_timed = True
                    native_request = state.owner.request_guarded
                    async def timed_native(method, params, before_write):
                        started, outcome = time.perf_counter(), 'error'
                        async def checked():
                            guard_started, guard_outcome = time.perf_counter(), 'refused'
                            try:
                                result = await before_write()
                                guard_outcome = 'allowed' if result is True else 'refused'
                                return result
                            finally:
                                span('native_final_authority', method, guard_started, guard_outcome)
                        try:
                            result = await native_request(method, params, checked)
                            outcome = 'acknowledged'
                            return result
                        finally:
                            span('native_request_ack', method, started, outcome)
                    state.owner.request_guarded = timed_native
                state.adapter.record = replace(state.adapter.record, qualification_id='isolated-production-fixture',
                    qualification_date='2026-11-11', operations={'text': p.CapabilityEntry(p.Support.QUALIFIED, evidence=('isolated fixture only',))})
                value = {'qualified_fixture': True}
            elif method in ('qualify_interrupt', 'lose_interrupt_answer', 'lose_broker_interrupt_answer'):
                state.adapter.record = replace(state.adapter.record, operations={**state.adapter.record.operations,
                    'interrupt': p.CapabilityEntry(p.Support.QUALIFIED, evidence=('isolated native interrupt fixture only',))})
                state.fixture_interrupts, state.fixture_completions = [], []
                state.fixture_interrupt_waiting = False
                state.fixture_interrupt_admissions = []
                interrupt_journal_root = state.journal.root
                interrupt_proof = {'interrupts': state.fixture_interrupts, 'completions': state.fixture_completions,
                    'writeAdmissions': state.fixture_interrupt_admissions}
                original_request, original_observe = state.owner.request_guarded, state.owner.rpc.observe
                async def request(method, params, before_write):
                    if method == 'turn/interrupt':
                        state.fixture_interrupt_waiting = True
                    async def checked():
                        allowed = await before_write()
                        if allowed is True and method == 'turn/interrupt':
                            state.fixture_interrupt_admissions.append(dict(params))
                        return allowed
                    value = await original_request(method, params, checked)
                    if method == 'turn/interrupt':
                        state.fixture_interrupts.append(dict(params))
                        if lose_answer:
                            raise ConnectionError('Artificial loss after actual native interrupt answer')
                    return value
                lose_answer = method == 'lose_interrupt_answer'
                if method == 'lose_broker_interrupt_answer':
                    original_dispatch = state.dispatch_grant
                    async def dispatch(grant):
                        operation = await state.peer.call('authority.resolve', {'grant': grant})
                        result = await original_dispatch(grant)
                        if operation['method'] == 'turn.interrupt' and result['outcome'] == 'accepted':
                            raise ConnectionError('Artificial loss after durable broker interrupt settlement')
                        return result
                    state.dispatch_grant = dispatch
                async def observe(event):
                    if event.get('method') == 'turn/completed':
                        turn = event.get('params', {}).get('turn', {})
                        state.fixture_completions.append({'id': turn.get('id'), 'status': turn.get('status')})
                    await original_observe(event)
                state.owner.request_guarded, state.owner.rpc.observe = request, observe
                value = {'qualified_fixture': True}
            elif method == 'hold_message_publication':
                message_release.clear()
                original_publication = state.peer.call
                async def held_message(method, params):
                    if method == 'publication' and params.get('kind') == 'message' and params.get('role', 'assistant') == 'assistant':
                        message_waiting.set()
                        await asyncio.wait_for(message_release.wait(), 30)
                    return await original_publication(method, params)
                state.peer.call = held_message
                value = {'held': True}
            elif method == 'message_publication_state':
                value = {'waiting': message_waiting.is_set() and not message_release.is_set()}
            elif method == 'release_message_publication':
                message_release.set()
                value = {'released': True}
            elif method == 'hold_native_write':
                await state.owner.rpc.write_lock.acquire()
                state.fixture_write_held = True
                value = {'held': True}
            elif method == 'native_write_state':
                value = {'waiting': state.fixture_interrupt_waiting, 'interrupts': len(state.fixture_interrupts)}
            elif method == 'release_native_write':
                assert state.fixture_write_held
                state.fixture_write_held = False
                state.owner.rpc.write_lock.release()
                value = {'released': True}
            elif method == 'interrupt_state':
                # Read existing bounded evidence after public Stop retires the
                # runtime too. No journal creation, claim, settlement or replay.
                journal = sqlite3.connect((interrupt_journal_root / 'admission.sqlite3').as_uri() + '?mode=ro', uri=True)
                try:
                    receipts = journal.execute(
                        "SELECT a.operation,a.state FROM admission a JOIN observation o ON o.operation=a.operation "
                        "WHERE json_extract(o.identity,'$.method')='turn.interrupt' LIMIT 16").fetchall()
                finally:
                    journal.close()
                value = {**interrupt_proof,
                    'refusedGuards': sum(row['metric'] == 'native_final_authority' and row['method'] == 'turn/interrupt'
                        and row['outcome'] == 'refused' for row in timings),
                    'journalReceipts': [{'operationId': identifier, 'state': status} for identifier, status in receipts]}
            elif method == 'native_metadata_state':
                value = {**native_metadata, 'requests': len(requests),
                    'answers': getattr(last_state, 'fixture_answers', []),
                    'marker': Path('/home/probe/relay-permission-forbidden').exists()}
            elif method.startswith('native_metadata_'):
                native_metadata['fault'] = method.removeprefix('native_metadata_')
                value = {'armed': native_metadata['fault']}
            elif method == 'generation_state':
                value = {'count': len(last_state.owned_generations), 'requests': len(requests),
                    'names': [view.name for view, _host in last_state.owned_generations],
                    'retired': [host.retired for _view, host in last_state.owned_generations],
                    'live': [view.bootstrapped() for view, _host in last_state.owned_generations],
                    'recovery': last_state.writers.gate.recovery_required}
            elif method == 'generation_capture':
                from acc.session_relay.adapters.base import TerminalRun
                run = TerminalRun(state.spec.spec['session_id'], state.spec.run_id)
                screens = [await view.capture(run) for view, _host in state.owned_generations]
                value = {'readable': [bool(screen['text']) and 'First production Chat' in screen['text'] for screen in screens]}
            elif method == 'stdio_fault_state':
                value = dict(stdio_fault)
            elif method.startswith('stdio_fault_'):
                kind = method.removeprefix('stdio_fault_')
                assert kind in ('write-prefix', 'write-partial', 'read-eof', 'read-partial', 'lost-response')
                stdio_fault.update(kind=kind, writes=0, bytes=0, responses=0, native_result=False, id=None)
                rpc = state.owner.rpc
                rpc.timeout = 1  # bound the deliberately lost reply; no retry
                original_writer = rpc.writer
                class FaultWriter:
                    def write(self, data):
                        message = json.loads(data)
                        if message.get('method') == 'turn/start':
                            stdio_fault['writes'] += 1
                            stdio_fault['id'] = message['id']
                            if kind.startswith('write-'):
                                cut = 1 if kind == 'write-prefix' else len(data) // 2
                                stdio_fault['bytes'] += cut
                                original_writer.write(data[:cut])
                                original_writer.write_eof()
                                raise ConnectionError('Isolated native stdin truncation')
                            stdio_fault['bytes'] += len(data)
                        return original_writer.write(data)
                    def __getattr__(self, key):
                        return getattr(original_writer, key)
                rpc.writer = FaultWriter()
                value = {'armed': kind}
            elif method == 'hold_approval_write':
                original = state.owner.respond
                native_writer = state.owner.rpc.writer
                state.fixture_approval_waiting = False
                state.fixture_approval_wire = 0
                class CountWriter:
                    def write(self, data):
                        value = json.loads(data)
                        if isinstance(value.get('result'), dict) and 'decision' in value['result']:
                            state.fixture_approval_wire += 1
                        return native_writer.write(data)
                    def __getattr__(self, key):
                        return getattr(native_writer, key)
                state.owner.rpc.writer = CountWriter()
                async def hold(*args, **kwargs):
                    await state.owner.rpc.write_lock.acquire()
                    state.fixture_approval_waiting = True
                    state.fixture_approval_lock_held = True
                    return await original(*args, **kwargs)
                state.owner.respond = hold
                value = {'armed': True}
            elif method == 'approval_write_state':
                value = {'waiting': state.fixture_approval_waiting, 'writes': state.fixture_approval_wire}
            elif method == 'release_approval_write':
                if state is not None and getattr(state, 'fixture_approval_lock_held', False):
                    state.fixture_approval_lock_held = False
                    state.owner.rpc.write_lock.release()
                value = {'released': True}
            elif method == 'native_socket_capacity':
                from websockets.asyncio.client import unix_connect
                from websockets.exceptions import ConnectionClosed
                peers = []
                baseline = len(state.native_socket.clients)
                async def connect():
                    ws = await unix_connect(state.native_socket.connect_path)
                    await ws.send(json.dumps({'id': 1, 'method': 'initialize',
                        'params': {'clientInfo': {'name': 'codex-tui', 'version': '0.167.1'}}}))
                    reply = json.loads(await asyncio.wait_for(ws.recv(), 5))
                    assert 'result' in reply, reply
                    return ws
                try:
                    for _ in range(16 - baseline):
                        peers.append(await connect())
                    assert len(state.native_socket.clients) == 16
                    overflow = await unix_connect(state.native_socket.connect_path)
                    try:
                        await asyncio.wait_for(overflow.recv(), 5)
                        raise AssertionError('Native cap did not refuse overflow')
                    except ConnectionClosed as error:
                        assert error.rcvd.code == 1008
                    finally:
                        await overflow.close()
                    await peers.pop().close()
                    for _ in range(200):
                        if len(state.native_socket.clients) == 15:
                            break
                        await asyncio.sleep(.01)
                    assert len(state.native_socket.clients) == 15
                    peers.append(await connect())
                    assert len(state.native_socket.clients) == 16
                    await peers[0].send(json.dumps({'id': 2, 'method': 'turn/start', 'params': {}}))
                    assert 'error' in json.loads(await asyncio.wait_for(peers[0].recv(), 5))
                    value = {'capacity': 16, 'baseline_native_clients': baseline,
                        'extra_protocol_readers': len(peers), 'overflow_refused': True, 'disconnect_recovers': True,
                        'unowned_reader_cannot_mutate': True}
                finally:
                    for ws in peers:
                        await ws.close()
                    for _ in range(200):
                        if len(state.native_socket.clients) == baseline:
                            break
                        await asyncio.sleep(.01)
                    assert len(state.native_socket.clients) == baseline
            elif method.startswith('transport_fault_'):
                kind = method.removeprefix('transport_fault_')
                host = state.terminal_host
                await host._connect()  # fault the actual authorized owned opener
                assert host.writer is not None
                if kind == 'read-eof':
                    host.writer.close()
                    await host.writer.wait_closed()
                    for _ in range(200):
                        if host.partial:
                            break
                        await asyncio.sleep(.01)
                    assert host.partial
                elif kind in ('partial-header', 'partial-body'):
                    original_writer = host.writer
                    class FragmentWriter:
                        def write(self, data):
                            cut = 2 if kind == 'partial-header' else len(data) - 1
                            assert 0 < cut < len(data)
                            original_writer.write(data[:cut])
                        async def drain(self):
                            await original_writer.drain()
                            original_writer.close()
                            await original_writer.wait_closed()
                            raise ConnectionError('Isolated actual partial frame EOF')
                        def __getattr__(self, key):
                            return getattr(original_writer, key)
                    host.writer = FragmentWriter()
                else:
                    raise ValueError('Unknown transport fault')
                value = {'armed': kind}
            elif method == 'request_permission':
                permission_once = True
                state.fixture_answers = []
                original_respond = state.owner.respond_guarded
                async def record_answer(identifier, value, before_write):
                    result = await original_respond(identifier, value, before_write)
                    state.fixture_answers.append({'id': identifier, 'decision': value.get('decision')})
                    return result
                state.owner.respond_guarded = record_answer
                state.adapter.record = replace(state.adapter.record, operations={**state.adapter.record.operations,
                    'decision.tool_approval': p.CapabilityEntry(p.Support.QUALIFIED, evidence=('isolated fixture only',))})
                value = {'permission_armed': True}
            elif method == 'permission_state':
                value = {'marker_exists': Path('/home/probe/relay-permission-forbidden').exists(),
                    'decisions': len(state.adapter.decisions) if state else None, 'requests': len(requests),
                    'answers': getattr(state, 'fixture_answers', [])}
            elif method == 'controls':
                value = await controls(state, requests)
            elif method == 'timings':
                value = {'spans': timings, 'capacity': 512, 'clock': 'perf_counter; durations measured within each span, no model subtraction'}
            elif method == 'slow_native_reader':
                view = state.owned_view
                peers = [peer for peer in view.native_socket.bootstrapped_clients if view.accepts(peer)]
                assert len(peers) == 1 and view.peer_fd is not None
                peer = peers[0]
                peer_identity = view.peer
                # Accelerate only this private lab's transport timers. Respect
                # production's no-pong-deadline choice, if configured.
                peer.ping_interval = .02
                if peer.ping_timeout is not None:
                    peer.ping_timeout = .04
                peer.keepalive_task.cancel()
                await asyncio.gather(peer.keepalive_task, return_exceptions=True)
                peer.start_keepalive()
                observations = []
                for _ in range(20):
                    signal.pidfd_send_signal(view.peer_fd, signal.SIGSTOP)
                    try:
                        await asyncio.sleep(.15)
                        sent = peer.protocol.close_sent
                        observations.append({'socket_open': peer.state.name == 'OPEN',
                            'heartbeat_timeout': bool(sent and sent.reason == 'keepalive ping timeout'),
                            'native_alive': not bool(select.select([view.peer_fd], [], [], 0)[0])})
                    finally:
                        signal.pidfd_send_signal(view.peer_fd, signal.SIGCONT)
                    await asyncio.sleep(.05)
                    if not observations[-1]['socket_open']:
                        break
                value = {'cycles': observations, 'same_peer': peer in view.native_socket.bootstrapped_clients,
                    'same_process': view.peer == peer_identity and not bool(select.select([view.peer_fd], [], [], 0)[0]),
                    'socket_open': peer.state.name == 'OPEN'}
            elif method == 'state':
                value = {'active': len(factory.active), 'active_turn': state.adapter.active_turn if state else None,
                    'processed': state.terminal_host.processed if state and state.terminal_host else None,
                    'processingLost': getattr(state, 'fixture_processing_lost', False), 'request_markers': list(requests), 'requests': len(requests), 'modelHeld': model_waiting.is_set() and not model_release.is_set(), 'start': {k: v for k, v in (state.native_start or {}).get('thread', {}).items() if k in ('ephemeral', 'path', 'source', 'turns')} if state else None}
            elif method == 'lose_authority':
                state.peer.invalidate()
                value = {'disconnected': True}
            elif method == 'end_owner':
                process = state.owner.runtime.process
                process.stdin.close()
                value = {'exited': True, 'returnCode': await asyncio.wait_for(process.wait(), 5)}
            elif method == 'crash_owner':
                process = state.owner.runtime.process
                # The new per-run supervisor is not the vendor. Pin only its
                # exact direct child with the qualified executable digest.
                children = Path(f'/proc/{process.pid}/task/{process.pid}/children').read_text().split()
                candidates = [int(pid) for pid in children
                    if p.sha256_digest(Path(f'/proc/{pid}/exe').read_bytes()) == CLI_DIGEST]
                assert len(candidates) == 1
                child = candidates[0]
                before = Path(f'/proc/{child}/stat').read_text().rsplit(')', 1)[1].split()
                assert int(before[1]) == process.pid
                descriptor = os.pidfd_open(child)
                try:
                    after = Path(f'/proc/{child}/stat').read_text().rsplit(')', 1)[1].split()
                    assert after[19] == before[19] and int(after[1]) == process.pid
                    signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                    await asyncio.wait_for(process.wait(), 5)
                    assert select.select([descriptor], [], [], 0)[0]
                    status = state.owner.runtime.native_exit_status()
                    assert status == -signal.SIGKILL
                    value = {'exited': True, 'returnCode': status}
                finally:
                    os.close(descriptor)
            elif method == 'crash_view_supervisor':
                from acc.broker.relay_enclosure import incarnation
                view = state.owned_view
                started = json.loads((view.enclosure.path / 'started.json').read_text())
                identity = f"{started['pid']}:{started['started']}"
                assert view.enclosure.proof('started.json', identity)
                fd = os.pidfd_open(started['pid'])
                try:
                    assert incarnation(started['pid']) == started['started']
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
                    for _ in range(500):
                        if select.select([fd], [], [], 0)[0]:
                            break
                        await asyncio.sleep(.01)
                    assert select.select([fd], [], [], 0)[0]
                finally:
                    os.close(fd)
                value = {'exited': True}
            elif method == 'crash_producer_vendor':
                from acc.broker.relay_enclosure import incarnation
                runtime = state.owner.runtime
                root = runtime.process.pid
                assert runtime.enclosure.proof('started.json', runtime.token)
                children = Path(f'/proc/{root}/task/{root}/children').read_text().split()
                assert len(children) == 1, children
                child = int(children[0])
                started = incarnation(child)
                fd = os.pidfd_open(child)
                try:
                    assert incarnation(child) == started
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
                finally:
                    os.close(fd)
                value = {'killed': True, 'supervisor': root}
            elif method == 'cleanup_state':
                views = [view for view, _host in last_state.owned_generations]
                value = {'active': len(factory.active), 'producer': last_state.owner.runtime.exit_proven,
                    'views': [view.exit_proven for view in views]}
            elif method == 'exit_state':
                runtime = last_state.owner.runtime
                value = {'owner': last_state.owner.attestation.process_identity,
                    'root_returncode': runtime.process.returncode, 'rpc_closed': last_state.owner.rpc.closed,
                    'empty': runtime.exit_proven if runtime.closed else runtime.enclosure.proof('exit.json', runtime.token),
                    'active': len(factory.active), 'requests': len(requests)}
            elif method == 'crash_producer_supervisor':
                runtime = state.owner.runtime
                runtime.process.kill()
                # wait() also waits for pipes retained by the native child.
                # Outer disposable PID namespace owns final cleanup here.
                for _ in range(500):
                    if runtime.process.returncode is not None:
                        break
                    await asyncio.sleep(.01)
                assert runtime.process.returncode == -9
                value = {'root_returncode': runtime.process.returncode}
            elif method == 'crash_view':
                view = state.owned_view
                assert view.peer_fd is not None and view.peer
                signal.pidfd_send_signal(view.peer_fd, signal.SIGKILL)
                for _ in range(500):
                    if select.select([view.peer_fd], [], [], 0)[0]:
                        break
                    await asyncio.sleep(.01)
                assert select.select([view.peer_fd], [], [], 0)[0]
                value = {'exited': True}
            elif method == 'hold_connect':
                original_connect = state.terminal_host._connect
                async def held_connect():
                    await original_connect()
                    original_write = state.terminal_host.writer.write
                    def tracked_write(frame):
                        connect_written[0] += len(frame)
                        return original_write(frame)
                    state.terminal_host.writer.write = tracked_write
                    connect_waiting.set()
                    await asyncio.wait_for(connect_release.wait(), 15)
                state.terminal_host._connect = held_connect
                value = {'fault_armed': True}
            elif method == 'connect_state':
                value = {'waiting': connect_waiting.is_set(), 'written': connect_written[0]}
            elif method == 'release_connect':
                connect_release.set()
                value = {'released': True}
            elif method == 'lose_input_ack':
                original_write = state.terminal_host.write_frames
                async def lost_input_ack(grant, sequence, frame):
                    result = await original_write(grant, sequence, frame)
                    if sequence == 2:
                        # Lose only the framed delivery answer, after actual native
                        # admission/processing. Never fabricate a native receipt.
                        for _ in range(500):
                            if state.terminal_host.processed == sequence:
                                break
                            await asyncio.sleep(.02)
                        assert state.terminal_host.processed == sequence
                        state.fixture_input_ack_lost = True
                        raise ConnectionError('Artificial lost framed input acknowledgement')
                    return result
                state.terminal_host.write_frames = lost_input_ack
                value = {'fault_armed': True}
            elif method == 'lose_processing':
                original_call = state.peer.call
                async def lost_processing(method, params):
                    if method == 'native.processed':
                        state.fixture_processing_lost = True
                        raise ConnectionError('Artificial lost processing acknowledgement')
                    return await original_call(method, params)
                state.peer.call = lost_processing
                value = {'fault_armed': True}
            elif method == 'hold_model':
                model_release.clear()
                value = {'held': True}
            elif method == 'release_model':
                model_release.set()
                value = {'released': True}
            elif method == 'clients':
                value = {'nativeId': state.binding.native_conversation, 'owner': state.owner.attestation.process_identity,
                    'views': [{'name': view.name, 'peer': view.peer, 'draft': host.draft, 'retired': host.retired,
                        'admitted': host.admitted, 'processed': host.processed, 'fresh': host.grant is None and not host.ticket}
                        for view, host in state.owned_generations]}
            elif method == 'screen':
                capture = ops.tmux(config, 'capture-pane', '-p', '-J', '-S', '-100', '-t', '=' + state.owned_view.name + ':')
                value = {'text': capture.stdout, 'stderr': capture.stderr, 'code': capture.returncode}
            elif method == 'finish':
                finished.set()
                value = {'done': True}
            else:
                raise ValueError('Unknown private fixture inspection')
        except Exception as error:
            if method == 'slow_native_reader':
                (Path(data['run_dir']) / 'relay-native-control-error.json').write_text(json.dumps({
                    'type': type(error).__name__, 'stack': [{'function': frame.name, 'line': frame.lineno}
                        for frame in traceback.extract_tb(error.__traceback__)[-8:]]}))
            value = {'error': repr(error)}
        writer.write(json.dumps(value).encode() + b'\n')
        await writer.drain()
        writer.close()
        await writer.wait_closed()
    inspector = await asyncio.start_unix_server(inspect, '/evidence/inspect.sock')
    Path('/evidence/ready.json').write_text(json.dumps({'base_url': f'http://127.0.0.1:{port}/v1'}))
    try:
        await asyncio.wait_for(finished.wait(), data.get('timeout', 100))
        for _ in range(500):
            if not factory.active:
                break
            await asyncio.sleep(.02)
        assert not factory.active
        print(json.dumps({'requests': requests, 'terminalMediated': False, 'timings': timings}))
    finally:
        server.close()
        inspector.close()
        model_server.close()
        await server.wait_closed()
        await inspector.wait_closed()
        await model_server.wait_closed()
        await asyncio.to_thread(ops.tmux, config, 'kill-server')


if __name__ == '__main__':
    asyncio.run(main())
