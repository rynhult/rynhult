"""Offline stock TUI -> unchanged broker attach/tmux byte path -> causal Host proof.

Only account/config command runners and main authority/admission are fixtures.
The attach bridge, PTY executor, tmux, CLI, private app-server and decoder are real.
"""
import asyncio
import errno
import fcntl
import json
import os
import pty
import re
import select
import socket
import struct
import subprocess
import sys
import termios
import types
from pathlib import Path

from process_cleanup import stop_group

# Minimal artificial placement avoids importing live account/provider bootstrap.
# All tmux invocations still execute the real installed binary in this namespace.
ops = types.ModuleType('acc.broker.ops')
ops.TMUX_NAME = re.compile(r'acc-[a-z0-9-]+')
ops.OpError = RuntimeError

def tmux(config, *args, check=False):
    return subprocess.run(['/usr/bin/tmux', '-L', config.tmux_socket, '-f', '/dev/null', *args],
                          text=True, capture_output=True, check=check, timeout=5)
ops.tmux = tmux
config_module = types.ModuleType('acc.broker.config')
config_module.BrokerConfig = types.SimpleNamespace
sys.modules['acc.broker.ops'] = ops
sys.modules['acc.broker.config'] = config_module

from acc.broker.attach import frame, read_frame  # noqa: E402
from acc.broker.relay_observer import OwnedObserver  # noqa: E402
from acc.broker.relay_tui import OwnedTUI  # noqa: E402
from acc.session_relay import protocol as p  # noqa: E402
from acc.session_relay.adapters.base import InputGrant, TerminalRun  # noqa: E402
from acc.session_relay.adapters.codex import StdioRPC  # noqa: E402
from acc.session_relay.native_input_host import NativeInputHost  # noqa: E402
from acc.session_relay.native_socket import NativeSocket  # noqa: E402
from acc.session_relay.writer_routes import WriterRoutes  # noqa: E402


async def main():
    assert not Path('/srv/acc').exists()
    assert not any(Path(os.environ['CODEX_HOME']).iterdir())
    assert {name for _, name in socket.if_nameindex()} == {'lo'}
    with socket.socket() as check:
        assert check.connect_ex(('192.0.2.1', 443)) == errno.ENETUNREACH
    count = 0
    async def model(reader, writer):
        nonlocal count
        try:
            header = await reader.readuntil(b'\r\n\r\n')
            size = next(int(line.split(b':', 1)[1]) for line in header.split(b'\r\n') if line.lower().startswith(b'content-length:'))
            assert size <= 1 << 20
            await reader.readexactly(size)
            count += 1
            message = {'type': 'message', 'id': 'msg_' + str(count), 'role': 'assistant', 'status': 'completed',
                       'content': [{'type': 'output_text', 'text': 'Host fixture response ' + str(count) + '.', 'annotations': []}]}
            response = {'id': 'resp_' + str(count), 'object': 'response', 'created_at': 1, 'status': 'completed',
                        'model': 'fixture-model', 'output': [message], 'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}
            events = [
                {'type': 'response.created', 'response': {**response, 'status': 'in_progress', 'output': []}},
                {'type': 'response.output_item.added', 'output_index': 0, 'item': {**message, 'status': 'in_progress', 'content': []}},
                {'type': 'response.output_text.delta', 'output_index': 0, 'content_index': 0, 'item_id': message['id'], 'delta': message['content'][0]['text']},
                {'type': 'response.output_item.done', 'output_index': 0, 'item': message},
                {'type': 'response.completed', 'response': response},
            ]
            body = ''.join('event: ' + event['type'] + '\ndata: ' + json.dumps(event) + '\n\n' for event in events).encode()
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\nContent-Length: '
                         + str(len(body)).encode() + b'\r\n\r\n' + body)
            await writer.drain()
        finally:
            writer.close()
    model_server = await asyncio.start_server(model, '127.0.0.1', 0)
    port = model_server.sockets[0].getsockname()[1]
    executable = Path('/evidence/native-path').read_text()
    owner = await asyncio.create_subprocess_exec(executable, 'app-server', '--listen', 'stdio://',
        '-c', 'model_provider="fixture"', '-c', 'model="fixture-model"', '-c',
        'model_providers.fixture={name="Offline fixture",base_url="http://127.0.0.1:' + str(port)
        + '/v1",wire_api="responses",requires_openai_auth=false,request_max_retries=0,stream_max_retries=0}',
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    proxy = host = bridge = observer = consume = view = readonly_bridge = readonly_consume = None
    bridge_tasks = set()
    master = None
    config = types.SimpleNamespace(tmux_socket='relay-native-host-fixture')
    session = 'acc-native-host-fixture'
    events, reads, admissions, processing, peers = [], [], [], [], []
    output, host_output, readonly_output = bytearray(), bytearray(), bytearray()
    async def observe(event):
        events.append(event.get('method'))
        if proxy:
            await proxy.publish(event.get('method'), event.get('params', {}))
    rpc = StdioRPC(owner.stdout, owner.stdin, observe)
    try:
        initialized = await rpc.request('initialize', {'clientInfo': {'name': 'fixture-host', 'version': '0'}, 'capabilities': {'experimentalApi': True}})
        await rpc.notify('initialized', {})
        started = await rpc.request('thread/start', {'cwd': '/home/probe', 'model': 'fixture-model', 'approvalPolicy': 'untrusted', 'sandbox': 'read-only'})
        native = started['thread']['id']
        async def submit(text):
            return await rpc.request('turn/start', {'threadId': native, 'input': [{'type': 'text', 'text': text, 'text_elements': []}]})
        await submit('Initial isolated Host history')
        async def until(predicate):
            for _ in range(300):
                if predicate():
                    return
                await asyncio.sleep(.03)
            pane = tmux(config, 'capture-pane', '-p', '-t', session).stdout[-4096:]
            raise TimeoutError('Isolated Host barrier did not complete: ' + json.dumps({
                'events': events, 'reads': reads, 'admissions': admissions, 'processing': processing,
                'host_processed': host.processed if host else None, 'pane': pane}))
        await until(lambda: events.count('turn/completed') == 1)
        async def allow(*_):
            return owner.returncode is None
        async def idle():
            value = await rpc.request('thread/read', {'threadId': native, 'includeTurns': True})
            return value['thread']['status']['type'] == 'idle'
        async def read(method, params):
            reads.append(method)
            if method == 'thread/resume':
                value = await rpc.request('thread/read', {'threadId': native, 'includeTurns': True})
                return {**started, 'thread': value['thread']}
            if method == 'model/list':
                return {'data': [{'id': 'fixture-model', 'model': 'fixture-model', 'displayName': 'Offline fixture',
                    'description': 'Synthetic', 'hidden': False, 'isDefault': True, 'defaultReasoningEffort': 'medium', 'supportedReasoningEfforts': []}], 'nextCursor': None}
            return await rpc.request(method, params)
        async def admit(actor, method, params):
            admissions.append({'actor': p.to_wire(actor), 'method': method, 'params': params})
            return {**await submit(params['text']), 'state': 'accepted'}
        async def processed(grant, sequence, result):
            processing.append({'epoch': grant.epoch, 'sequence': sequence, 'state': result['state']})
            return True  # synthetic main receipt bridge, explicitly not production attestation
        async def retire(lease):
            return await host.retire(lease)
        routes = WriterRoutes(run_id='fixture-run', execution_identity='owner-pid-' + str(owner.pid),
                              authorize=allow, retire=retire, admit=admit, read=read)
        chat_actor = p.Actor(p.ActorKind.PERSON, 'fixture-chat', p.Channel.CHAT)
        terminal_actor = p.Actor(p.ActorKind.PERSON, 'fixture-terminal', p.Channel.TERMINAL)
        # Component fixture starts with Terminal's lease before creating its
        # native client. It must never reactivate an already-retired Host.
        await routes.take(terminal_actor, 'terminal')
        directory = Path('/evidence/socket')
        directory.mkdir(mode=0o700)
        async def opener(run):
            return await view.open(run)
        host = NativeInputHost(TerminalRun('fixture-session', 'fixture-run'), opener, allow, allow,
            validate_read=allow, routes=routes, idle=idle, processed=processed, consume_output=False)
        def native_route(websocket):
            assert view.accepts(websocket)
            peers.append(view.peer[0])
            return host
        proxy = NativeSocket(directory, native, read, allow, initialized=initialized, start_settings=started, writer_route=native_route)
        await proxy.start()
        spec = types.SimpleNamespace(run_id='fixture-run', spec={'session_id': 'fixture-session'},
                                     binding=types.SimpleNamespace(workspace='/home/probe'))
        view = OwnedTUI(Path('/evidence/view'), config, spec, executable, proxy,
                        digest=p.sha256_digest(Path(executable).read_bytes()))
        await view.start(allow)
        session = view.name
        readonly_bridge = OwnedObserver(config, session, host.run, allow,
            lambda: view.peer_fd is not None and not select.select([view.peer_fd], [], [], 0)[0])
        readonly_reader, readonly_writer = await readonly_bridge.open(host.run)
        # An observer connection is read-only at the actual broker byte bridge,
        # even if it attempts a raw framed write without a Terminal grant.
        readonly_writer.write(frame(b'd', b'ungranted observer bytes\r'))
        await readonly_writer.drain()
        async def readonly_pump():
            try:
                while True:
                    kind, data = await read_frame(readonly_reader)
                    if kind == b'd' and len(readonly_output) < 128 << 10:
                        readonly_output.extend(data)
                    if kind == b'x':
                        return
            except (asyncio.IncompleteReadError, ConnectionError):
                return
        readonly_consume = asyncio.create_task(readonly_pump())
        client = Path('/proc') / str(view.peer[0])
        environment_keys = {entry.split(b'=', 1)[0] for entry in (client / 'environ').read_bytes().split(b'\0') if entry}
        mountinfo = (client / 'mountinfo').read_text().splitlines()
        isolation = {
            'separate_network': (client / 'ns/net').readlink() != Path('/proc/self/ns/net').readlink(),
            'account_root_absent': not (client / 'root/srv/acc').exists(),
            'credentials_absent': not environment_keys.intersection({b'ANTHROPIC_API_KEY', b'OPENAI_API_KEY', b'CODEX_API_KEY'}),
            'config_readonly': any(' /home/relay/.codex/config.toml ro,' in line for line in mountinfo),
            'workspace_readonly': any(' /home/probe ro,' in line for line in mountinfo),
        }
        assert all(isolation.values()), isolation
        master, slave = pty.openpty()
        os.set_blocking(master, False)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 32, 120, 0, 0))
        def terminal():
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
        observer = await asyncio.create_subprocess_exec('/usr/bin/tmux', '-L', config.tmux_socket, 'attach-session', '-t', session, '-r',
            stdin=slave, stdout=slave, stderr=slave, preexec_fn=terminal, env={**os.environ, 'TERM': 'xterm-256color'})
        os.close(slave)
        async def output_pump():
            async for data in host.attach_read(host.run):
                if len(host_output) < 128 << 10:
                    host_output.extend(data)
        consume = asyncio.create_task(output_pump())
        await until(lambda: 'thread/items/list' in reads)
        await asyncio.sleep(.5)
        grant = InputGrant(host.run, 'fixture-attachment', terminal_actor, 'fixture-stream', 1, p.Fence(1, 'broker', 'main'))
        await host.claim_input(grant)
        rejected_controls = []
        for label, data in (
            ('model', b'/model fixture'), ('shell', b'!echo artificial'),
            ('approval', b'/approvals'), ('escape', b'\x1b[A'),
            ('paste', b'\x1b[200~artificial\x1b[201~'), ('unicode', 'artificial é'.encode()),
            ('multiline', b'first\rsecond\r'),
        ):
            try:
                await host.write_frames(grant, 1, data)
            except p.RelayError as error:
                assert error.code is p.ErrorCode.UNSUPPORTED
                rejected_controls.append(label)
            else:
                raise AssertionError('Unsupported control reached the byte path: ' + label)
            assert host.admitted == host.processed == 0 and host.draft == '' and host.ticket is None
        typed = await host.write_frames(grant, 1, b'Exact Host native input')
        refused_drain = False
        try:
            await host.drain(grant, 1)
        except p.RelayError:
            refused_drain = True
        await asyncio.sleep(.7)
        entered = await host.write_frames(grant, 2, b'\r')
        await until(lambda: host.processed == 2 and events.count('turn/completed') == 2)
        drained = await host.drain(grant, 2)
        chat = await routes.take(chat_actor, 'chat')
        refused_old_input = False
        try:
            await host.write_frames(grant, 3, b'late input')
        except p.RelayError:
            refused_old_input = True
        await routes.mutate(chat, 'turn.submit', {'text': 'Chat after genuine Host retirement'})
        await until(lambda: events.count('turn/completed') == 3)
        for _ in range(50):
            try:
                data = os.read(master, 65536)
                output.extend(data)
                if b'\x1b[6n' in data:
                    os.write(master, b'\x1b[1;1R')
            except BlockingIOError:
                pass
            if all(b'Host fixture response 3.' in value for value in (readonly_output, host_output, output)):
                break
            await asyncio.sleep(.03)
        assert b'Host fixture response 3.' in readonly_output
        captured = await view.capture(host.run)
        assert 'Host fixture response 3.' in captured['text']
        assert captured['truncated'] is False
        assert routes.snapshot()['writer'] == 'chat'  # read cannot steal native input
        await readonly_bridge.close()
        await readonly_consume
        view_exit_proven = await view.close()
        assert view_exit_proven
        print(json.dumps({'view_isolation': isolation, 'view_exit_proven': view_exit_proven, 'nativeId': native, 'owner_pid': owner.pid, 'owner_alive': owner.returncode is None,
            'rejected_controls': rejected_controls, 'owned_placement': True, 'peers': peers, 'reads': reads, 'admissions': admissions, 'processing': processing,
            'typed': p.to_wire(typed), 'entered': p.to_wire(entered), 'drained': p.to_wire(drained),
            'refused_drain_before_decoder': refused_drain, 'refused_old_input': refused_old_input,
            'writer': routes.snapshot(), 'fixture_requests': count, 'turns_started': events.count('turn/started'),
            'turns_completed': events.count('turn/completed'), 'terminalMediated': False,
            'owned_capture_live_chat': 'Host fixture response 3.' in captured['text'],
            'owned_observer_live_chat': b'Host fixture response 3.' in readonly_output, 'framed_output_bytes': len(host_output), 'observer_output_bytes': len(output),
            'framed_live_chat': b'Host fixture response 3.' in host_output,
            'observer_live_chat': b'Host fixture response 3.' in output,
            'scope': 'real unchanged attach/pty_exec/tmux/native decoder and causal Host consumption; artificial account/config/authority/admission/receipt bridge; no production attestation'}))
    finally:
        if readonly_bridge:
            await readonly_bridge.close()
        if readonly_consume:
            await asyncio.gather(readonly_consume, return_exceptions=True)
        if consume:
            consume.cancel()
            await asyncio.gather(consume, return_exceptions=True)
        if host:
            await host.detach(host.run)
        if view:
            assert await view.close(), "Owned view tree exit was not proven"
        if bridge:
            bridge.close()
            await bridge.wait_closed()
        await asyncio.gather(*tuple(bridge_tasks), return_exceptions=True)
        tmux(config, 'kill-server')
        await stop_group(observer)
        if master is not None:
            os.close(master)
        if proxy:
            await proxy.close()
        await rpc.close()
        if owner.returncode is None:
            owner.terminate()
            await owner.wait()
        model_server.close()
        await model_server.wait_closed()


if __name__ == '__main__':
    asyncio.run(main())
