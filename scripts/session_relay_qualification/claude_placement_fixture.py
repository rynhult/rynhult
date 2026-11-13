"""Production account broker/owned tmux inside the enforced offline fixture.

No authority overrides or synthetic Host callbacks; diagnostic wrapper only. The parent supplies only the
artificial main's normal broker RPCs. Inspection is private test evidence only.
"""
import asyncio
import json
import os
import pwd
from pathlib import Path

from acc.broker import ops
from acc.broker.config import BrokerConfig
from acc.broker.relay_runtime import RuntimeTransportFactory
from acc.broker.server import Broker


def native_tui(settings, env, observer_file, requests, native):
    before = len(requests)
    result = asyncio.run(serve(env))
    return {**result, 'requests': requests[before:]}


async def serve(env):
    data = json.loads(Path('/evidence/placement-config.json').read_text())
    assert pwd.getpwuid(os.getuid()).pw_dir == '/home/probe'
    # Artificial trust preferences for the disposable workspace only.
    for file in (Path('/home/probe/.claude.json'), Path(os.environ['CLAUDE_CONFIG_DIR']) / '.claude.json'):
        value = json.loads(file.read_text())
        value.setdefault('projects', {})[data['workspace']] = {'hasTrustDialogAccepted': True}
        file.write_text(json.dumps(value))
    config = BrokerConfig(socket_path=Path('/evidence/owned-broker.sock'),
        run_dir=Path(data['run_dir']), allowed_roots=(Path(data['workspace']),),
        allowed_uids=frozenset({os.getuid()}), providers={'claude': '/usr/bin/claude'},
        tmux_socket='relay-placement-offline', hook_path='/evidence/installed/relay-hook')
    from acc.broker import claude_auth
    original_probe = claude_auth.probe
    def diagnostic_probe(*args, **kwargs):
        result = original_probe(*args, **kwargs)
        if result != 'api_key':
            with Path('/evidence/placement-errors.log').open('a') as stream:
                stream.write('Native auth classification: ' + result + '\n')
        return result
    claude_auth.probe = diagnostic_probe
    factory = RuntimeTransportFactory(config, owned_view_factory=None)
    replacement_armed, replacement_ready = asyncio.Event(), asyncio.Event()
    if data.get('unknown_tree'):
        from acc.broker.claude_owner import TmuxProcess
        retire = TmuxProcess.retire_view
        async def retire_after_replacement(process):
            if replacement_armed.is_set():
                await replacement_ready.wait()
            return await retire(process)
        TmuxProcess.retire_view = retire_after_replacement
    # Test diagnostics only; preserve every production result/exception.
    import traceback

    from acc.broker.relay_ownership import ExclusiveRuntime
    from acc.session_relay.runtime_peer import RuntimePeer
    rpc_inflight = {}
    interrupt_lock = {'task': None, 'entered': None, 'release': None}
    read_race = {"token": None, "checks": 0, "injected": 0, "held": None}
    original_call = RuntimePeer.call
    async def traced_call(peer, method, params):
        rpc_inflight[method] = rpc_inflight.get(method, 0) + 1
        try:
            result = await original_call(peer, method, params)
            if method == 'authority.owner_observation' and Path('/evidence/arm-observation-race').exists():
                token = params['observation']
                if read_race['token'] != token:
                    read_race.update(token=token, checks=0)
                read_race['checks'] += 1
                if read_race['checks'] == 2:
                    state = next(iter(factory.active.values()))
                    assert not state.operation_lock.locked()
                    await state.operation_lock.acquire()
                    read_race.update(held=state, injected=read_race['injected'] + 1)
                    Path('/evidence/arm-observation-race').unlink()
            return result
        finally:
            rpc_inflight[method] -= 1
    RuntimePeer.call = traced_call
    # Fault only the exact guard process created by the production broker.
    from acc.broker.claude_guard import InstalledGuard
    original_guard = InstalledGuard.check
    guard_kills = []
    async def guard_check(self, payload, environment, cwd):
        armed = Path('/evidence/kill-next-installed-guard')
        if not armed.exists():
            return await original_guard(self, payload, environment, cwd)
        armed.unlink()
        create = asyncio.create_subprocess_exec
        async def kill_created(*args, **kwargs):
            process = await create(*args, **kwargs)
            if args and args[0] == str(self.path):
                process.kill()
                await process.wait()
                assert process.returncode == -9
                guard_kills.append(process.pid)
            return process
        asyncio.create_subprocess_exec = kill_created
        try:
            return await original_guard(self, payload, environment, cwd)
        finally:
            asyncio.create_subprocess_exec = create
    InstalledGuard.check = guard_check
    from acc.broker.claude_owner import ClaudeOwnerFactory
    sdk_metadata = {'kind': None, 'changes': 0}
    original_chat = ClaudeOwnerFactory.chat
    async def trace_chat(self, *args, **kwargs):
        launched = await original_chat(self, *args, **kwargs)
        adapter = self.current
        query = adapter.client.query
        async def trace_query(*values, **options):
            self.runtime.fixture_query_count = getattr(self.runtime, 'fixture_query_count', 0) + 1
            return await query(*values, **options)
        adapter.client.query = trace_query
        # The joined SDK keeps a single receive_messages reader through result
        # and background drain. Inject on that exact stream, never an unused API.
        receive = adapter.client.receive_messages
        async def metadata_receive():
            from dataclasses import replace

            from claude_agent_sdk import AssistantMessage, ResultMessage, ToolResultBlock, UserMessage
            async for message in receive():
                kind = sdk_metadata['kind']
                duplicate = False
                if kind and not sdk_metadata['changes']:
                    changed = None
                    if isinstance(message, ResultMessage) and kind in ('result-session', 'result-failed'):
                        changed = replace(message, **({'session_id': '33333333-3333-4333-8333-333333333333'}
                            if kind == 'result-session' else {'subtype': 'error_during_execution', 'is_error': True}))
                    elif isinstance(message, AssistantMessage) and kind == 'parent-tool' and any(type(block).__name__ == 'ToolUseBlock' for block in message.content):
                        changed = replace(message, parent_tool_use_id='old-incarnation-tool')
                    elif isinstance(message, UserMessage) and isinstance(message.content, list) and any(isinstance(block, ToolResultBlock) for block in message.content):
                        if kind == 'tool-reference':
                            changed = replace(message, content=[replace(block, tool_use_id='old-incarnation-tool') if isinstance(block, ToolResultBlock) else block for block in message.content])
                        elif kind == 'result-uuid':
                            changed = replace(message, uuid=None)
                        elif kind == 'duplicate-result':
                            changed, duplicate = message, True
                    if changed is not None:
                        message = changed
                        sdk_metadata['changes'] += 1
                yield message
                if duplicate:
                    yield message
        adapter.client.receive_messages = metadata_receive
        return launched
    ClaudeOwnerFactory.chat = trace_chat
    original_sdk_publish = ClaudeOwnerFactory.sdk_publish
    sdk_messages = []
    sdk_progress = []
    rpc_failures = []
    async def sdk_publish(self, event):
        if event['kind'] == 'provider_message':
            assert len(sdk_messages) < 128
            message = event['message']
            content = message.get('content')
            sdk_messages.append({'type': event['sdk_type'], 'keys': sorted(message),
                'blocks': [{key: block.get(key) for key in ('type', 'id', 'tool_use_id', 'is_error', 'content') if key in block}
                    for block in content if isinstance(block, dict)] if isinstance(content, list) else []})
        kind = event['kind']
        if kind in ('operation.accepted', 'turn.completed'):
            assert len(sdk_progress) < 128
            sdk_progress.append({'kind': kind, 'phase': 'start', 'operationId': event['operationId']})
        result = await original_sdk_publish(self, event)
        if kind in ('operation.accepted', 'turn.completed'):
            sdk_progress.append({'kind': kind, 'phase': 'done', 'operationId': event['operationId']})
        return result
    ClaudeOwnerFactory.sdk_publish = sdk_publish
    observations = []
    run_directories = set()
    original_observe = ExclusiveRuntime.observe
    async def observed(self, payload):
        result = await original_observe(self, payload)
        assert len(observations) < 128
        response = payload.get('tool_response')
        observations.append({'run_id': self.spec.run_id, 'native_id': payload.get('session_id'),
            'event': payload.get('hook_event_name'),
            'native_tool_id': payload.get('tool_use_id'),
            'payload_keys': sorted(payload),
            'tool_response_keys': sorted(response) if isinstance(response, dict) else [],
            'background_result': isinstance(response, dict) and bool(response.get('backgroundTaskId'))})
        return result
    ExclusiveRuntime.observe = observed
    original_handle = ExclusiveRuntime.handle
    async def diagnostic(self, method, params):
        try:
            result = await original_handle(self, method, params)
            if method == 'terminal.input' and getattr(self, 'fixture_lose_input_ack', False):
                self.fixture_lose_input_ack = False
                for _ in range(500):
                    native = self.terminal_host.on_processed.__self__
                    if self.terminal_host.processed == params['sequence'] and native.pending is None:
                        break
                    await asyncio.sleep(.01)
                assert self.terminal_host.processed == params['sequence'] and native.pending is None
                raise ConnectionError('Isolated ACK lost after real native processing')
            return result
        except Exception as error:
            if len(rpc_failures) < 128:
                rpc_failures.append({'method': method, 'type': type(error).__name__,
                    'code': getattr(getattr(error, 'code', None), 'name', None)})
            with Path('/evidence/placement-errors.log').open('a') as stream:
                stream.write(traceback.format_exc())
                for file in config.run_dir.rglob('started.json'):
                    record = json.loads(file.read_text())
                    stream.write(json.dumps({k: record.get(k) for k in ('pid', 'started', 'runId')}) + '\n')
                panes = ops.tmux(config, 'list-panes', '-a', '-F', '#{pane_pid}:#{pane_dead}:#{pane_current_command}')
                stream.write(panes.stdout)
                screen = ops.tmux(config, 'capture-pane', '-p', '-J', '-S', '-30', '-t', '=' + self.spec.spec.get('tmux_name', data['tmux_name']) + ':')
                stream.write(screen.stdout[-6000:])
            raise
        finally:
            if method == 'owner.inspect' and read_race['held'] is self:
                self.operation_lock.release()
                read_race['held'] = None
            if self.spec is not None:
                directory = Path(self.spec.spec['run_dir']).resolve()
                assert directory.is_relative_to(config.run_dir.resolve())
                run_directories.add(directory)
    ExclusiveRuntime.handle = diagnostic
    broker = Broker(config, relay_transport=factory)
    server = await asyncio.start_unix_server(broker.handle, str(config.socket_path))
    finish = asyncio.Event()
    errors, records = [], []
    async def inspect(reader, writer):
        try:
            method = json.loads(await asyncio.wait_for(reader.readline(), 3))['method']
            state = next(iter(factory.active.values()), None)
            owner = state.owners.current if state else None
            if method == 'state':
                host = getattr(owner, 'host', None)
                result = {'rpc_inflight': sorted(method for method, count in rpc_inflight.items() if count), 'active': len(factory.active), 'view': state.controller.switch.view if state and state.controller else None,
                    'read_race_injected': read_race['injected'],
                    'application_observers': len(state.application_tasks) if state else 0,
                    'application_retained': len(state.application_records) if state else 0,
                    'processed': getattr(host, 'processed', None), 'admitted': getattr(host, 'admitted', None),
                    'idle': getattr(owner, 'idle', None), 'failed': getattr(owner, 'failed', None),
                    'native_transcript': bool(state.native_transcript) if state else False,
                    'guard_helper_kills': len(guard_kills),
                    'sdk_queries': getattr(state, 'fixture_query_count', 0),
                    'sdk_interrupts': list(getattr(getattr(getattr(owner, 'client', None), '_transport', None), 'interrupts', {}).values()),
                    'owner_failure': getattr(owner, 'failure', getattr(owner, 'failed', None)),
                    'owner_done': owner.owner.done() if owner is not None and getattr(owner, 'owner', None) is not None else None,
                    'owner_process': (getattr(getattr(owner, 'attestation', None), 'process_identity', None)
                        or (f'{owner.process.pid}:{owner.process.started}' if owner is not None
                            and hasattr(getattr(owner, 'process', None), 'started') else None)),
                    'enclosures': len(state.owners.enclosures) if state else 0,
                    'model_held': Path('/evidence/model-waiting').exists() and Path('/evidence/hold-model').exists(),
                    'hook_events': [event['event'] for event in observations],
                    'hook_observations': observations,
                    'sdk_messages': sdk_messages, 'sdk_metadata_changes': sdk_metadata['changes'],
                    'sdk_progress': sdk_progress,
                    'rpc_failures': rpc_failures,
                    'guard_denials': [record['guard']['rule'] for directory in run_directories
                        for file in [directory / 'events.jsonl'] if file.is_file()
                        for record in [json.loads(line) for line in file.read_text().splitlines()] if isinstance(record.get('guard'), dict)],
                    'permission_armed': Path('/evidence/request-native-permission').exists(),
                    'background': len(getattr(owner, 'background', ())),
                    'background_results': [event for event in observations if event['background_result']],
                    'background_started': (Path(data['workspace']) / 'native-background-started').exists(),
                    'background_finished': (Path(data['workspace']) / 'native-background-finished').exists()}
            elif method in ('kill_native_owner', 'kill_owner_supervisor'):
                import signal

                from acc.broker.relay_enclosure import incarnation
                if data.get('unknown_tree') and method == 'kill_native_owner':
                    replacement_armed.set()
                process = owner.process
                root = process.pid
                enclosure = state.owners.enclosures[state.spec.run_id]
                identity = f'{root}:{incarnation(root)}'
                assert enclosure.proof('started.json', identity)
                children = Path(f'/proc/{root}/task/{root}/children').read_text().split()
                assert len(children) == 1, children
                child = int(children[0])
                target = root if method == 'kill_owner_supervisor' else child
                fd = os.pidfd_open(target)
                try:
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
                finally:
                    os.close(fd)
                result = {'killed': target, 'supervisor': root, 'runId': state.spec.run_id}
            elif method in ('replace_owned_pane', 'replace_owned_server'):
                process = owner.process
                assert isinstance(process, __import__('acc.broker.claude_owner', fromlist=['TmuxProcess']).TmuxProcess)
                enclosure = state.owners.enclosures[state.spec.run_id]
                assert enclosure.proof('exit.json', f'{process.pid}:{process.started}')
                if method == 'replace_owned_server':
                    import select

                    from acc.broker.relay_enclosure import incarnation
                    assert incarnation(process.server_pid) == process.server_started
                    old_server = os.pidfd_open(process.server_pid)
                    try:
                        assert incarnation(process.server_pid) == process.server_started
                        await asyncio.to_thread(ops.tmux, config, 'kill-server')
                        async with asyncio.timeout(5):
                            while not select.select([old_server], [], [], 0)[0]:
                                await asyncio.sleep(.01)
                    finally:
                        os.close(old_server)
                    value = await asyncio.to_thread(ops.tmux, config, 'new-session', '-d', '-s', process.name, '/usr/bin/cat')
                else:
                    value = await asyncio.to_thread(ops.tmux, config, 'new-window', '-d', '-t', process.session_id,
                        '-n', 'replacement-proof', '/usr/bin/cat')
                    assert value.returncode == 0
                    value = await asyncio.to_thread(ops.tmux, config, 'select-window', '-t', '=' + process.name + ':replacement-proof')
                assert value.returncode == 0, (value.returncode, value.stdout, value.stderr)
                value = await asyncio.to_thread(ops.tmux, config, 'display-message', '-p', '-t', '=' + process.name + ':', '#{pane_pid}')
                state.fixture_replacement_pid = int(value.stdout.strip())
                assert state.fixture_replacement_pid != process.pid
                replacement_ready.set()
                result = {'replaced': True}
            elif method == 'replacement_state':
                pid = state.fixture_replacement_pid
                try:
                    os.kill(pid, 0)
                    live = True
                except ProcessLookupError:
                    live = False
                value = await asyncio.to_thread(ops.tmux, config, 'capture-pane', '-p', '-t', '=' + state.spec.spec['tmux_name'] + ':')
                result = {'live': live, 'forbidden_input': 'Never write into replacement pane' in value.stdout}
            elif method == 'owner_exit':
                process = owner.process
                enclosure = state.owners.enclosures[state.spec.run_id]
                started = json.loads((enclosure.path / 'started.json').read_text())
                try:
                    root_live = __import__('acc.broker.relay_enclosure', fromlist=['incarnation']).incarnation(started['pid']) == started['started']
                except (FileNotFoundError, ProcessLookupError):
                    root_live = False
                result = {'root_live': root_live, 'empty': enclosure.proof('exit.json', f'{started["pid"]}:{started["started"]}'),
                    'failure': getattr(owner, 'failure', getattr(owner, 'failed', None)),
                    'done': owner.owner.done() if getattr(owner, 'owner', None) is not None else None}
            elif method == 'replay_old_publication':
                from acc.session_relay import protocol as p
                paths = list((config.run_dir / 'abc123def456' / 'relay' / 'application-proofs').glob('*.json'))
                assert len(paths) == 1
                fact = json.loads(paths[0].read_text())
                denied = 0
                for run_id in (fact['operation']['runId'], state.spec.run_id):
                    try:
                        await state.peer.call('publication', {'kind': 'decision.applied',
                            'runId': run_id, 'operationId': fact['operation']['operationId'],
                            'application': fact['application']})
                    except (ConnectionError, p.RelayError):
                        denied += 1
                    else:
                        raise AssertionError('Old publication accepted by fresh enrollment')
                result = {'refused': denied, 'peer_live': not state.peer.closed}
            elif method.startswith('sdk_metadata_'):
                kind = method.removeprefix('sdk_metadata_')
                assert kind in ('result-session', 'result-failed', 'parent-tool', 'tool-reference', 'result-uuid', 'duplicate-result')
                sdk_metadata['kind'] = kind
                result = {'armed': kind}
            elif method == 'observer_capacity':
                from acc.session_relay import protocol as p
                observer = state.owners.observer
                assert observer is not None and not observer.closed
                baseline = len(observer.tasks)
                streams = []
                try:
                    for _ in range(16 - baseline):
                        streams.append(await observer.open(observer.run))
                    assert len(observer.tasks) == 16
                    try:
                        await observer.open(observer.run)
                        raise AssertionError('Native observer overflow admitted')
                    except p.RelayError as error:
                        assert error.code is p.ErrorCode.LIMIT
                    _reader, freed = streams.pop()
                    freed.close()
                    await freed.wait_closed()
                    for _ in range(300):
                        if len(observer.tasks) == 15:
                            break
                        await asyncio.sleep(.01)
                    assert len(observer.tasks) == 15
                    streams.append(await observer.open(observer.run))
                    assert len(observer.tasks) == 16
                    result = {'cap': 16, 'baseline': baseline, 'recovered': True}
                finally:
                    for _reader, stream in streams:
                        stream.close()
                    await asyncio.gather(*(stream.wait_closed() for _reader, stream in streams), return_exceptions=True)
                    for _ in range(300):
                        if len(observer.tasks) == baseline:
                            break
                        await asyncio.sleep(.01)
                    assert len(observer.tasks) == baseline
            elif method == 'kill_next_installed_guard':
                Path('/evidence/kill-next-installed-guard').touch()
                result = {'armed': True}
            elif method == 'kill_permission_hook':
                import signal

                import psutil

                from acc.broker.relay_enclosure import incarnation
                root = owner.process.pid
                channel = str(state.owners.hooks.directory / 'owner-channel.json')
                matches = [child for child in psutil.Process(root).children(recursive=True)
                    if child.cmdline() and child.cmdline()[-1] == channel
                    and any('acc.session_relay.claude_hook' in arg for arg in child.cmdline())]
                assert len(matches) == 1, len(matches)
                pid = matches[0].pid
                started = incarnation(pid)
                fd = os.pidfd_open(pid)
                try:
                    assert incarnation(pid) == started
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
                finally:
                    os.close(fd)
                result = {'killed': True}
            elif method == 'sdk_partial_write':
                transport = owner.client._transport
                original_write = transport.write
                used = False
                async def truncate_query(data):
                    nonlocal used
                    if not used:
                        used = True
                        assert data.startswith('{')
                        await original_write(data[:1])  # real stdin, incomplete JSON record
                        await transport._stdin_stream.aclose()
                        raise ConnectionError('Isolated actual SDK stdin truncation')
                    return await original_write(data)
                transport.write = truncate_query
                result = {'armed': True}
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
                        def __init__(self):
                            self.truncated = False
                        def write(self, data):
                            cut = 2 if kind == 'partial-header' else len(data) - 1
                            assert 0 < cut < len(data)
                            original_writer.write(data[:cut])
                            self.truncated = True
                        async def drain(self):
                            await original_writer.drain()
                            original_writer.close()
                            await original_writer.wait_closed()
                            raise ConnectionError('Isolated actual partial frame EOF')
                        def __getattr__(self, key):
                            return getattr(original_writer, key)
                    host.writer = FragmentWriter()
                elif kind == 'lost-ack':
                    state.fixture_lose_input_ack = True
                else:
                    raise ValueError('Unknown transport fault')
                result = {'armed': kind}
            elif method.startswith('source_fault_'):
                kind = method.removeprefix('source_fault_')
                assert state.native_transcript[0] == state.spec.run_id
                path = Path(state.native_transcript[1])
                raw = path.read_bytes()
                assert raw and path.is_relative_to('/home/probe')
                if kind == 'truncate':
                    path.write_bytes(b'')
                elif kind == 'rewrite':
                    assert b'Native TUI isolated fixture turn' in raw
                    path.write_bytes(raw.replace(b'Native TUI isolated fixture turn', b'AlterX TUI isolated fixture turn', 1))
                elif kind == 'replace':
                    old = path.with_suffix('.old')
                    path.rename(old)
                    path.write_bytes(raw)
                    path.chmod(0o600)
                elif kind == 'symlink':
                    old = path.with_suffix('.old')
                    path.rename(old)
                    path.symlink_to(old)
                elif kind == 'mode':
                    path.chmod(0o666)
                else:
                    raise ValueError('Unknown source fault')
                result = {'tampered': kind}
            elif method == 'arm_observation_race':
                Path('/evidence/arm-observation-race').touch()
                result = {'armed': True}
            elif method == 'decision_metadata':
                # Synthetic isolated transcript only; never an account/session outside this fixture.
                source = state.native_transcript if state else None
                rows = []
                if source and Path(source[1]).is_file():
                    path = Path(source[1])
                    assert path.stat().st_size <= 1 << 20
                    for line in path.read_text().splitlines():
                        row = json.loads(line)
                        content = row.get('message', {}).get('content')
                        if isinstance(content, list):
                            for block in content:
                                if isinstance(block, dict) and block.get('type') == 'tool_result':
                                    rows.append({'uuid': row.get('uuid'), 'parentUuid': row.get('parentUuid'),
                                        'toolUseResult': row.get('toolUseResult'), 'result': block})
                result = {'records': rows[-8:]}
            elif method == 'request_background':
                Path('/evidence/request-native-background').touch()
                result = {'armed': True}
            elif method == 'release_background':
                (Path(data['workspace']) / 'release-native-background').touch()
                result = {'released': True}
            elif method == 'permission_state':
                result = {'marker_exists': (Path(data['workspace']) / 'relay-native-permission-forbidden').exists()}
            elif method == 'request_guard':
                (Path(data['workspace']) / '.env').write_text('synthetic-fixture-not-a-secret')
                Path('/evidence/request-native-guard').touch()
                Path('/evidence/request-native-permission').touch()
                Path('/evidence/request-native-public-permission').touch()
                result = {'armed': True}
            elif method == 'request_public_permission':
                Path('/evidence/request-native-permission').touch()
                Path('/evidence/request-native-public-permission').touch()
                result = {'armed': True}
            elif method == 'request_permission':
                Path('/evidence/request-native-permission').touch()
                result = {'armed': True}
            elif method == 'hold_long_public_model':
                Path('/evidence/long-hold-model').touch()
                Path('/evidence/public-hold-model').touch()
                Path('/evidence/model-waiting').unlink(missing_ok=True)
                Path('/evidence/hold-model').touch()
                result = {'held': True}
            elif method == 'hold_public_model':
                Path('/evidence/public-hold-model').touch()
                Path('/evidence/model-waiting').unlink(missing_ok=True)
                Path('/evidence/hold-model').touch()
                result = {'held': True}
            elif method == 'sdk_interrupt_lock':
                interrupt_lock['entered'], interrupt_lock['release'] = asyncio.Event(), asyncio.Event()
                lock = owner.client._transport.base._write_lock
                async def hold_interrupt_lock():
                    async with lock:
                        interrupt_lock['entered'].set()
                        await asyncio.wait_for(interrupt_lock['release'].wait(), 10)
                interrupt_lock['task'] = asyncio.create_task(hold_interrupt_lock())
                await interrupt_lock['entered'].wait()
                result = {'locked': True}
            elif method == 'sdk_interrupt_unlock':
                interrupt_lock['release'].set()
                await interrupt_lock['task']
                interrupt_lock['task'] = None
                result = {'released': True}
            elif method == 'hold_model':
                Path('/evidence/model-waiting').unlink(missing_ok=True)
                Path('/evidence/hold-model').touch()
                result = {'held': True}
            elif method == 'release_model':
                Path('/evidence/hold-model').unlink(missing_ok=True)
                Path('/evidence/public-hold-model').unlink(missing_ok=True)
                result = {'released': True}
            elif method == 'screen':
                value = await asyncio.to_thread(ops.tmux, config, 'capture-pane', '-p', '-J', '-S', '-100', '-t', '=' + (state.spec.spec.get('tmux_name') if state else data['tmux_name']) + ':')
                result = {'text': value.stdout, 'returncode': value.returncode}
            elif method == 'finish':
                result = {'done': True}
                finish.set()
            else:
                raise ValueError('Unknown inspection method')
        except Exception as error:
            if method.startswith('replace_owned_'):
                replacement_ready.set()
            errors.append(repr(error))
            import traceback
            result = {'error': repr(error), 'trace': traceback.format_exc()}
        writer.write(json.dumps(result).encode() + b'\n')
        await writer.drain()
        writer.close()
        await writer.wait_closed()
    inspector = await asyncio.start_unix_server(inspect, '/evidence/owned-inspect.sock')
    ready = Path('/evidence/owned-ready.tmp')
    ready.write_text(json.dumps({'base_url': env['ANTHROPIC_BASE_URL']}))
    os.replace(ready, ready.with_suffix('.json'))
    try:
        await asyncio.wait_for(finish.wait(), min(220, int(data.get('timeout', 100))))
        for _ in range(500):
            if not factory.active:
                break
            await asyncio.sleep(.02)
        if data.get('unknown_tree'):
            assert len(factory.active) == 1, 'Unknown old tree must retain its conversation quarantine'
        else:
            assert not factory.active, 'Parent must close the authenticated production runtime'
        for directory in run_directories:
            for file in directory.rglob('exit.json'):
                record = json.loads(file.read_text())
                records.append({'empty': record.get('empty'), 'runId': record.get('runId'), 'status': record.get('status')})
        assert (data.get('unknown_tree') or records or not any(list(directory.rglob('started.json')) for directory in run_directories)) and all(record['empty'] for record in records)
    finally:
        server.close()
        inspector.close()
        await server.wait_closed()
        await inspector.wait_closed()
        await asyncio.to_thread(ops.tmux, config, 'kill-server')
    assert not errors
    return {'unknown_tree_quarantined': bool(data.get('unknown_tree') and len(factory.active) == 1), 'production_placement': True, 'production_opener': True, 'empty_tree_records': records,
            'hook_observations': observations,
                    'sdk_messages': sdk_messages,
                    'guard_denials': [record['guard']['rule'] for directory in run_directories
                        for file in [directory / 'events.jsonl'] if file.is_file()
                        for record in [json.loads(line) for line in file.read_text().splitlines()] if isinstance(record.get('guard'), dict)], 'terminalMediated': False, 'scope': 'actual production account broker/factory/SDK/tmux/Host/opaque grants; artificial main principal, no browser'}
