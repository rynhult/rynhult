"""Run only inside probe.sandbox: stock native TUI + private real app-server."""
import asyncio
import errno
import fcntl
import hashlib
import json
import os
import pty
import signal
import socket
import struct
import termios
from pathlib import Path

from acc.session_relay import protocol as p
from acc.session_relay.adapters.codex import StdioRPC
from acc.session_relay.native_socket import NativeSocket
from acc.session_relay.writer_lease import WriterRetirement
from acc.session_relay.writer_routes import WriterRoutes


async def main():
    assert not Path('/srv/acc').exists()
    assert not any(Path(os.environ['CODEX_HOME']).iterdir())
    assert {name for _, name in socket.if_nameindex()} == {'lo'}
    with socket.socket() as check:
        assert check.connect_ex(('192.0.2.1', 443)) == errno.ENETUNREACH
    writer_mode = Path('/evidence/writer-mode').exists()
    guard_mode = Path('/evidence/guard-mode').exists()
    guard_tampered = Path('/evidence/guard-tampered').exists()
    guard_run = Path('/home/probe/run')
    guard_environment = {}
    hook_hash = None
    if guard_mode:
        guard_run.mkdir(mode=0o700)
        policy = guard_run / 'guardrails.json'
        policy.write_text(json.dumps({'workspace': '/home/probe', 'custom': [{
            'pattern': 'relay-native-forbidden', 'reason': 'Native fixture denies this command.'}]}))
        wrapper = Path('/evidence/guard_fixture.py')
        wrapper.write_text("import runpy,sys; sys.argv=['relay-guard-fixture','codex','PreToolUse']; "
                           "g=runpy.run_path('/evidence/guard_source.py'); raise SystemExit(g['main']())\n")
        command = '/usr/bin/python3 -I /evidence/guard_fixture.py'
        hooks_file = Path(os.environ['CODEX_HOME']) / 'hooks.json'
        events = ('SessionStart', 'SessionEnd', 'UserPromptSubmit', 'PermissionRequest', 'Stop', 'Interrupt',
                  'PreToolUse', 'PostToolUse')
        labels = ('session_start', 'session_end', 'user_prompt_submit', 'permission_request', 'stop', 'interrupt',
                  'pre_tool_use', 'post_tool_use')
        hooks_config = {}
        trust = '[features]\nhooks = true\n'
        hook_hashes = {}
        for event, label in zip(events, labels):
            hook = {'type': 'command', 'command': command if event == 'PreToolUse' else '/usr/bin/true'}
            hooks_config[event] = [{'hooks': [hook]}]
            identity = {'event_name': label, 'hooks': [{**hook, 'async': False,
                'timeout': 1 if event in ('SessionEnd', 'Interrupt') else 600}]}
            digest = 'sha256:' + hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            key = str(hooks_file) + ':' + label + ':0:0'
            hook_hashes[key] = digest
            trust += '[hooks.state.' + json.dumps(key) + ']\ntrusted_hash = ' + json.dumps(digest) + '\n'
        hook_hash = hook_hashes[str(hooks_file) + ':pre_tool_use:0:0']
        hooks_file.write_text(json.dumps({'hooks': hooks_config}))
        (hooks_file.parent / 'config.toml').write_text(trust)
        guard_environment = {'ACC_RUN_DIR': str(guard_run), 'ACC_SESSION_ID': 'fixture',
            'ACC_GUARD_FILES': 'guardrails.json=' + hashlib.sha256(policy.read_bytes()).hexdigest()}
        if guard_tampered:
            policy.write_text('{}')  # frozen launch digest must refuse this change
    fixture_requests = []
    fixture_tools = []
    guard_seen_by_model = False
    async def fixture(reader, writer):
        try:
            header = await reader.readuntil(b'\r\n\r\n')
            length = next((int(line.split(b':', 1)[1]) for line in header.split(b'\r\n')
                           if line.lower().startswith(b'content-length:')), 0)
            nonlocal guard_seen_by_model
            raw = await reader.readexactly(length)
            payload = json.loads(raw)
            fixture_requests.append(len(raw))
            fixture_tools.extend(tool.get('name', '') for tool in payload.get('tools', [])
                                 if tool.get('type') == 'function')
            guard_seen_by_model |= ('were changed after launch' if guard_tampered else 'Native fixture denies this command.') in raw.decode()
            response = {'id': 'resp_fixture', 'object': 'response', 'created_at': 1,
                'status': 'completed', 'model': 'fixture-model', 'output': [
                    {'id': 'msg_fixture', 'type': 'message', 'status': 'completed', 'role': 'assistant',
                     'content': [{'type': 'output_text', 'text': 'Isolated fixture response ' + str(len(fixture_requests)) + '.', 'annotations': []}]}],
                'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}
            message = response['output'][0]
            part = message['content'][0]
            stream = [
                {'type': 'response.created', 'response': {**response, 'status': 'in_progress', 'output': []}},
                {'type': 'response.output_item.added', 'output_index': 0,
                 'item': {**message, 'status': 'in_progress', 'content': []}},
                {'type': 'response.content_part.added', 'output_index': 0, 'content_index': 0,
                 'item_id': message['id'], 'part': {**part, 'text': ''}},
                {'type': 'response.output_text.delta', 'output_index': 0, 'content_index': 0,
                 'item_id': message['id'], 'delta': part['text']},
                {'type': 'response.output_text.done', 'output_index': 0, 'content_index': 0,
                 'item_id': message['id'], 'text': part['text']},
                {'type': 'response.content_part.done', 'output_index': 0, 'content_index': 0,
                 'item_id': message['id'], 'part': part},
                {'type': 'response.output_item.done', 'output_index': 0, 'item': message},
                {'type': 'response.completed', 'response': response},
            ]
            if guard_mode and len(fixture_requests) == 1:
                name = next((name for name in fixture_tools if name in ('exec_command', 'shell_command', 'shell')), None)
                assert name is not None, fixture_tools
                command = 'printf fixture > /home/probe/relay-native-forbidden'
                arguments = {'cmd': command} if name == 'exec_command' else (
                    {'command': command} if name == 'shell_command' else {'command': ['/bin/sh', '-c', command]})
                call = {'id': 'fc_guard_fixture', 'type': 'function_call', 'call_id': 'call_guard_fixture',
                        'name': name, 'arguments': json.dumps(arguments), 'status': 'completed'}
                response['output'] = [call]
                stream = [
                    {'type': 'response.created', 'response': {**response, 'status': 'in_progress', 'output': []}},
                    {'type': 'response.output_item.added', 'output_index': 0,
                     'item': {**call, 'arguments': '', 'status': 'in_progress'}},
                    {'type': 'response.function_call_arguments.delta', 'output_index': 0,
                     'item_id': call['id'], 'delta': call['arguments']},
                    {'type': 'response.function_call_arguments.done', 'output_index': 0,
                     'item_id': call['id'], 'arguments': call['arguments']},
                    {'type': 'response.output_item.done', 'output_index': 0, 'item': call},
                    {'type': 'response.completed', 'response': response},
                ]
            body = ''.join('event: ' + event['type'] + '\ndata: ' + json.dumps(
                {**event, 'sequence_number': number}) + '\n\n' for number, event in enumerate(stream)).encode()
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\nContent-Length: '
                         + str(len(body)).encode() + b'\r\n\r\n' + body)
            await writer.drain()
        finally:
            writer.close()
    fixture_server = await asyncio.start_server(fixture, '127.0.0.1', 0)
    fixture_port = fixture_server.sockets[0].getsockname()[1]
    executable = Path('/evidence/native-path').read_text()
    server = await asyncio.create_subprocess_exec(executable, 'app-server', '--listen', 'stdio://',
        '-c', 'model_provider="fixture"', '-c', 'model="fixture-model"',
        '-c', 'model_providers.fixture={name="Offline fixture",base_url="http://127.0.0.1:' + str(fixture_port) + '/v1",wire_api="responses",requires_openai_auth=false,request_max_retries=0,stream_max_retries=0}',
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        env={**os.environ, **guard_environment})
    proxy = None
    captured = []
    requests = []
    replies = []
    read_errors = []
    events = []
    async def observe(event):
        events.append(event.get('method'))
        if proxy:
            await proxy.publish(event.get('method'), event.get('params', {}))
    rpc = StdioRPC(server.stdout, server.stdin, observe)
    class Traced:
        def __init__(self, websocket):
            self.websocket = websocket
        def __getattr__(self, name):
            return getattr(self.websocket, name)
        async def send(self, raw):
            if len(replies) < 64:
                replies.append(json.loads(raw))
            await self.websocket.send(raw)
        async def __aiter__(self):
            async for raw in self.websocket:
                if len(requests) < 64:
                    value = json.loads(raw)
                    requests.append({'id': value.get('id'), 'method': value.get('method'), 'params': value.get('params')})
                yield raw
    class CapturedSocket(NativeSocket):
        async def connection(self, websocket):
            await super().connection(Traced(websocket))
    tui = None
    master = None
    try:
        initialized = await rpc.request('initialize', {'clientInfo': {'name': 'rynhult-offline-fixture', 'version': '0'}, 'capabilities': {'experimentalApi': True}})
        await rpc.notify('initialized', {})
        hooks = await rpc.request('hooks/list', {'cwds': ['/home/probe']}) if guard_mode else None
        started = await rpc.request('thread/start', {'model': 'fixture-model', 'cwd': '/home/probe',
            'approvalPolicy': 'never' if guard_mode else 'untrusted',
            'sandbox': 'danger-full-access' if guard_mode else 'read-only'})
        native = started['thread']['id']
        # Materialize genuine history without a live provider: the namespace's
        # local synthetic Responses endpoint completes this artificial turn.
        await rpc.request('turn/start', {'threadId': native, 'input': [{'type': 'text', 'text': 'isolated history fixture', 'text_elements': []}]})
        for _ in range(100):
            if 'turn/completed' in events:
                break
            await asyncio.sleep(.1)
        assert 'turn/completed' in events, events
        async def read(method, params):
            captured.append(method)
            if method == 'thread/resume':
                # A read-only projection of this same newly owned server thread;
                # never forward a resume/config mutation from the TUI.
                return started
            if method == 'model/list':
                return {'data': [{'id': 'fixture-model', 'model': 'fixture-model', 'displayName': 'Offline fixture',
                    'description': 'Synthetic catalog; no inference', 'hidden': False, 'isDefault': True,
                    'defaultReasoningEffort': 'medium', 'supportedReasoningEfforts': [{'reasoningEffort': 'medium', 'description': 'Fixture'}]}], 'nextCursor': None}
            try:
                return await rpc.request(method, params)
            except Exception as error:
                read_errors.append({'method': method, 'error': str(error)[:400]})
                raise
        async def authorize():
            return server.returncode is None
        writer_routes = None
        terminal_route = None
        chat_route = None
        admissions = []
        retirements = []
        peer_pids = []
        closed_routes = set()
        async def admit(actor, method, params):
            assert method == 'turn.submit'
            admissions.append({'actor': p.to_wire(actor), 'method': method, 'params': params})
            result = await rpc.request('turn/start', {'threadId': native, 'input': [
                {'type': 'text', 'text': params['text'], 'text_elements': []}]})
            return {**result, 'state': 'accepted'}
        async def retire(lease):
            value = await rpc.request('thread/read', {'threadId': native, 'includeTurns': True})
            idle = value['thread']['status']['type'] == 'idle'
            # This fixture owns all typed input and has no unsent input before
            # either transfer. It does not qualify a production draft observer.
            closed_routes.add(lease.handle)
            proof = WriterRetirement(lease.handle, idle, True, not writer_routes.unsettled,
                lease.handle in closed_routes, 'stdio-pid-' + str(server.pid))
            retirements.append({'epoch': lease.epoch, 'idle': idle, 'view': lease.view})
            return proof
        chat_actor = p.Actor(p.ActorKind.PERSON, 'fixture-chat-person', p.Channel.CHAT)
        terminal_actor = p.Actor(p.ActorKind.PERSON, 'fixture-terminal-person', p.Channel.TERMINAL)
        if writer_mode:
            writer_routes = WriterRoutes(run_id='fixture-run', execution_identity='stdio-pid-' + str(server.pid),
                authorize=lambda _actor: authorize(), retire=retire, admit=admit, read=read)
            chat_route = await writer_routes.take(chat_actor, 'chat')
            terminal_route = await writer_routes.take(terminal_actor, 'terminal')
        def writer_route(websocket):
            if not writer_mode or tui is None or tui.returncode is not None:
                return None
            sock = websocket.transport.get_extra_info('socket')
            pid, uid, _gid = struct.unpack('@iII', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if pid != tui.pid or uid != os.getuid():
                return None
            peer_pids.append(pid)
            async def mutate(method, params):
                return await writer_routes.mutate(terminal_route, method, params)
            return mutate
        directory = Path('/evidence/socket')
        directory.mkdir(mode=0o700)
        proxy = CapturedSocket(directory, native, read, authorize, initialized=initialized, start_settings=started,
            writer_route=writer_route if writer_mode else None)
        await proxy.start()
        master, slave = pty.openpty()
        os.set_blocking(master, False)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 32, 120, 0, 0))
        def terminal():
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
        tui = await asyncio.create_subprocess_exec(executable if writer_mode else '/usr/bin/codex', '--remote', 'unix://' + proxy.connect_path,
            '--no-alt-screen', 'resume', native, stdin=slave, stdout=slave, stderr=slave,
            env={**os.environ, 'TERM': 'xterm-256color'}, preexec_fn=terminal)
        os.close(slave)
        output = b''
        entered = False
        live_update = False
        takeover = False
        stale_typed = False
        deadline = asyncio.get_running_loop().time() + 9
        while tui.returncode is None and asyncio.get_running_loop().time() < deadline and len(output) < 128 << 10:
            try:
                data = os.read(master, 65536)
                output += data
                if b'\x1b[6n' in data:
                    os.write(master, b'\x1b[1;1R')
            except BlockingIOError:
                pass
            except OSError:
                break
            if not entered and 'thread/items/list' in captured:
                await asyncio.sleep(.3)
                os.write(master, b'artificial authorized input' if writer_mode else b'artificial unauthorized input')
                await asyncio.sleep(.6)
                os.write(master, b'\r')
                entered = True
            native_turns = [value for value in requests if value.get('method') == 'turn/start']
            if writer_mode and entered and not takeover and len(native_turns) == 1 and events.count('turn/completed') == 2:
                chat_route = await writer_routes.take(chat_actor, 'chat')
                takeover = True
                await asyncio.sleep(.3)
                os.write(master, b'stale native input')
                await asyncio.sleep(.6)
                os.write(master, b'\r')
                stale_typed = True
            if entered and not live_update and ((not writer_mode and native_turns) or
                    (writer_mode and takeover and len(native_turns) == 2)):
                # Retained native reads must display the existing owner's next
                # Chat turn even after the old terminal lease has been retired.
                if writer_mode:
                    await writer_routes.mutate(chat_route, 'turn.submit', {'text': 'isolated live update fixture'})
                else:
                    await rpc.request('turn/start', {'threadId': native, 'input': [
                        {'type': 'text', 'text': 'isolated live update fixture', 'text_elements': []}]})
                live_update = True
            await asyncio.sleep(.03)
        # Same owner and conversation still answer structured reads, with no
        # second app-server or provider turn started by the native input.
        status = await rpc.request('thread/loaded/list', {})
        print(json.dumps({'nativeId': native, 'read_methods': captured, 'events': events, 'requests': requests, 'replies': replies, 'read_errors': read_errors,
            'owner_pid': server.pid, 'owner_alive': server.returncode is None,
            'loaded': status, 'terminal_output_bytes': len(output), 'terminal_startup': output.decode(errors='replace')[:64000],
            'native_input_attempted': entered, 'live_update_attempted': live_update,
            'writer': {'mode': writer_mode, 'admissions': admissions, 'retirements': retirements,
                'peer_pids': peer_pids, 'tui_pid': tui.pid, 'takeover': takeover, 'stale_typed': stale_typed,
                'snapshot': writer_routes.snapshot() if writer_routes else None},
            'fixture_request_count': len(fixture_requests), 'fixture_tools': sorted(set(fixture_tools)),
            'guard': {'mode': guard_mode, 'tampered': guard_tampered, 'hooks': hooks, 'expected_hook_hash': hook_hash, 'expected_hook_hashes': hook_hashes if guard_mode else {},
                'seen_by_model': guard_seen_by_model,
                'forbidden_file_exists': Path('/home/probe/relay-native-forbidden').exists(),
                'records': [json.loads(line) for line in (guard_run / 'events.jsonl').read_text().splitlines()]
                    if (guard_run / 'events.jsonl').exists() else []}, 'terminalMediated': proxy.attested,
            'scope': 'real private app-server and stock TUI; genuine same-thread history and live update; synthetic local Responses and model catalog; native mutation refused; no live account or external inference'}))
    finally:
        if tui and tui.returncode is None:
            os.killpg(tui.pid, signal.SIGTERM)
            await asyncio.wait_for(tui.wait(), 5)
        if master is not None:
            os.close(master)
        if proxy:
            await proxy.close()
        fixture_server.close()
        await fixture_server.wait_closed()
        await rpc.close()
        if server.returncode is None:
            server.terminate()
            await asyncio.wait_for(server.wait(), 5)


if __name__ == '__main__':
    asyncio.run(main())
