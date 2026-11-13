"""OS-process account broker, pinned native owners/views; no external route/key."""
import asyncio
import errno
import fcntl
import json
import os
import socket
import sqlite3
import sys
import traceback
from dataclasses import replace
from pathlib import Path

from acc.broker import ops
from acc.broker.config import BrokerConfig
from acc.broker.relay_runtime import BrokerRuntime, RuntimeTransportFactory
from acc.broker.server import Broker
from acc.session_relay import protocol as p


def config():
    data = json.loads(Path('/evidence/config.json').read_text())
    return BrokerConfig(socket_path=Path('/evidence/broker.sock'), run_dir=Path(data['run_dir']),
        allowed_roots=(Path(data['workspace']),), allowed_uids=frozenset({os.getuid()}),
        providers={'codex': '/usr/bin/codex'}, tmux_socket='relay-lifetime', hook_path='/evidence/noop-hook')


async def rpc(path, method):
    reader, writer = await asyncio.open_unix_connection(path)
    try:
        writer.write(json.dumps({'method': method}).encode() + b'\n')
        await writer.drain()
        value = json.loads(await reader.readline())
        if 'error' in value:
            raise RuntimeError(value)
        return value
    finally:
        writer.close()
        await writer.wait_closed()


def snapshot(factory):
    status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
    return {'pid': os.getpid(), 'active': len(factory.active),
        'fds': len(list(Path('/proc/self/fd').iterdir())), 'tasks': len(asyncio.all_tasks()),
        'rss_kib': int(status['VmRSS'].split()[0]), 'threads': int(status['Threads']),
        'cpus': len(os.sched_getaffinity(0)),
        'runs': [{'key': state.key, 'owner': state.owner.runtime.process.pid,
                  'view': state.owned_view.peer[0], 'incarnation': state.broker.incarnation,
                  'native': state.binding.native_conversation} for state in factory.active.values()]}


async def worker():
    cfg = config()
    factory = RuntimeTransportFactory(cfg)
    tracked = []
    errors = []
    admission_losses = []
    handle = BrokerRuntime.handle
    async def diagnostic(self, method, params):
        try:
            return await handle(self, method, params)
        except Exception:
            error = {'method': method, 'key': self.key, 'traceback': traceback.format_exc()}
            if self.owned_view is not None:
                view = self.owned_view
                capture = await asyncio.to_thread(ops.tmux, cfg, 'capture-pane', '-p', '-J', '-S', '-50', '-t', '=' + view.name + ':')
                error['screen'] = capture.stdout[-4096:]
                error['view'] = {'closed': view.closed, 'peer': view.peer, 'bootstrapped': view.bootstrapped()}
            errors.append(error)
            del errors[:-16]
            raise
    BrokerRuntime.handle = diagnostic
    broker = Broker(cfg, relay_transport=factory)
    cfg.socket_path.unlink(missing_ok=True)
    Path('/evidence/worker.sock').unlink(missing_ok=True)
    server = await asyncio.start_unix_server(broker.handle, str(cfg.socket_path))
    async def inspect_value(method):
        if method == 'debug':
            value = {'errors': errors, 'admission_losses': admission_losses, 'tasks': [{'coro': repr(task.get_coro()), 'stack': [f'{frame.f_code.co_filename}:{frame.f_lineno}' for frame in task.get_stack()]} for task in asyncio.all_tasks()]}
        elif method == 'qualify':
            for state in factory.active.values():
                state.adapter.record = replace(state.adapter.record, qualification_id='group6-isolated-fixture',
                    qualification_date='2026-11-11', operations={'text': p.CapabilityEntry(p.Support.QUALIFIED, evidence=('fixture only',))})
            value = {'qualified': True}
        elif method == 'uncertain':
            state = next(iter(factory.active.values()))
            dispatch = state.dispatch
            async def lose_result(operation):
                result = await dispatch(operation)
                admission_losses.append({'operationId': operation.operation_id, 'outcome': result.outcome.value})
                assert result.outcome.value == 'accepted'
                raise ConnectionError('Fixture loss after native admission before journal settlement')
            state.dispatch = lose_result
            value = {'armed': True}
        elif method == 'idle':
            value = {'idle': all(state.adapter.active_turn is None for state in factory.active.values())}
        elif method == 'track':
            for state in factory.active.values():
                if state not in tracked:
                    # Exercise actual owned bridge tasks/socketpairs too. No
                    # input grant or native text is supplied by this inspector.
                    stream, bridge = await state.owned_view.open(state.terminal_host.run)
                    bridge.close()
                    await bridge.wait_closed()
                    tracked.append(state)
            value = {'tracked': len(tracked)}
        elif method == 'clean':
            value = {'clean': False, **snapshot(factory)}
            if tracked and all(state.cleanup_task and state.cleanup_task.done() for state in tracked):
                for state in tracked:
                    state.cleanup_task.result()
                    assert state.owner.runtime.closed and state.owner.runtime.process.returncode is not None
                    assert state.owned_view.exit_proven and state.owned_view.peer_fd is None
                    assert not state.owned_view.attachments and not state.owned_view.detachers
                    assert state.owned_view.enclosure.fd is None
                    assert state.native_socket.directory_fd is None and state.native_socket.server is None
                    assert not state.native_socket.clients and not state.native_socket.bootstrapped_clients
                    assert state.event_task.done() and state.exit_task.done()
                    for resource in (state.journal, state.staging):
                        try:
                            info = os.fstat(resource.fd)
                        except OSError:
                            pass
                        else:
                            assert (info.st_dev, info.st_ino) != (resource.identity.st_dev, resource.identity.st_ino)
                    lock = os.open(state.owner.runtime.root / 'owner.lock', os.O_RDWR)
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    finally:
                        os.close(lock)
                    try:
                        state.journal.db.execute('SELECT 1')
                    except sqlite3.ProgrammingError:
                        pass
                    else:
                        raise AssertionError('journal connection survived cleanup')
                value['clean'] = True
                value['count'] = len(tracked)
                tracked.clear()
        else:
            value = snapshot(factory)
        return value
    async def inspect(reader, writer):
        try:
            value = await inspect_value(json.loads(await reader.readline())['method'])
        except Exception as error:
            value = {'error': repr(error)}
        writer.write(json.dumps(value).encode() + b'\n')
        await writer.drain()
        writer.close()
        await writer.wait_closed()
    inspector = await asyncio.start_unix_server(inspect, '/evidence/worker.sock')
    try:
        await asyncio.Event().wait()
    finally:
        server.close()
        inspector.close()
        await server.wait_closed()
        await inspector.wait_closed()


async def controller():
    # Limit this disposable namespace only; stock Rust thread pools otherwise
    # scale to the host CPU count and obscure bounded per-session churn.
    os.sched_setaffinity(0, set(sorted(os.sched_getaffinity(0))[:8]))
    assert {name for _, name in socket.if_nameindex()} == {'lo'}
    with socket.socket() as check:
        assert check.connect_ex(('192.0.2.1', 443)) == errno.ENETUNREACH
    Path('/evidence/noop-hook').write_text('#!/bin/sh\nexit 0\n')
    Path('/evidence/noop-hook').chmod(0o500)
    cfg = config()
    ops.prepare_tmux(cfg)
    requests = []
    async def model(reader, writer):
        header = await reader.readuntil(b'\r\n\r\n')
        size = next(int(line.split(b':', 1)[1]) for line in header.split(b'\r\n') if line.lower().startswith(b'content-length:'))
        assert 0 < size <= 1 << 20
        payload = json.loads(await reader.readexactly(size))
        requests.append({'has_fixture': 'Lifetime fixture turn' in json.dumps(payload.get('input', []))})
        message = {'type': 'message', 'id': 'msg_fixture', 'role': 'assistant', 'status': 'completed',
            'content': [{'type': 'output_text', 'text': 'Offline lifetime response.', 'annotations': []}]}
        response = {'id': 'resp_fixture', 'object': 'response', 'created_at': 1, 'status': 'completed',
            'model': 'fixture-model', 'output': [message], 'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}
        events = [{'type': 'response.created', 'response': {**response, 'status': 'in_progress', 'output': []}},
            {'type': 'response.output_item.added', 'output_index': 0, 'item': {**message, 'status': 'in_progress', 'content': []}},
            {'type': 'response.output_text.delta', 'output_index': 0, 'content_index': 0, 'item_id': message['id'], 'delta': message['content'][0]['text']},
            {'type': 'response.output_item.done', 'output_index': 0, 'item': message}, {'type': 'response.completed', 'response': response}]
        body = ''.join('event: ' + event['type'] + '\ndata: ' + json.dumps(event) + '\n\n' for event in events).encode()
        writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\nContent-Length: '
            + str(len(body)).encode() + b'\r\n\r\n' + body)
        await writer.drain()
        writer.close()
    model_server = await asyncio.start_server(model, '127.0.0.1', 0)
    process = None
    killed_runs = []
    main_process = None
    main_killed = False
    async def start():
        nonlocal process
        Path('/evidence/worker.sock').unlink(missing_ok=True)
        process = await asyncio.create_subprocess_exec(sys.executable, '/evidence/driver.py', '--worker',
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL)
        for _ in range(500):
            assert process.returncode is None
            if Path('/evidence/worker.sock').exists():
                return
            await asyncio.sleep(.02)
        raise TimeoutError('Account broker startup')
    await start()
    async def start_main():
        nonlocal main_process
        Path('/evidence/main.sock').unlink(missing_ok=True)
        main_process = await asyncio.create_subprocess_exec(sys.executable, '/evidence/main_driver.py',
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL)
        for _ in range(500):
            assert main_process.returncode is None
            if Path('/evidence/main.sock').exists():
                return
            await asyncio.sleep(.02)
        raise TimeoutError('Private main startup')
    finished = asyncio.Event()
    async def inspect(reader, writer):
        nonlocal main_killed
        try:
            method = json.loads(await reader.readline())['method']
            if method == 'main_start':
                assert main_process is None or main_process.returncode is not None
                await start_main()
                value = {'started': main_process.pid}
            elif method == 'main_kill':
                assert main_process is not None and main_process.returncode is None
                main_process.kill()
                await main_process.wait()
                main_killed = True
                value = {'killed': main_process.pid, 'returncode': main_process.returncode}
            elif method.startswith('main:'):
                operation = method.removeprefix('main:')
                if operation == 'promote_fenced':
                    assert main_killed, 'No independent OS-main fencing receipt'
                value = await rpc('/evidence/main.sock', operation)
            elif method == 'kill':
                killed_runs.extend((await rpc('/evidence/worker.sock', 'state'))['runs'])
                process.kill()
                await process.wait()
                value = {'killed': process.pid}
            elif method == 'restart':
                assert process.returncode is not None
                await start()
                value = {'started': process.pid}
            elif method in ('state', 'track', 'clean', 'qualify', 'idle', 'uncertain', 'debug'):
                value = await rpc('/evidence/worker.sock', method)
                value['requests'] = len(requests)
                value['old_execution'] = [pid for run in killed_runs for pid in (run['owner'], run['view'])
                    if Path(f'/proc/{pid}/stat').exists()
                    and Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0] != 'Z']
            elif method == 'finish':
                value = {'requests': len(requests)}
                finished.set()
            else:
                raise ValueError(method)
        except Exception as error:
            value = {'error': repr(error)}
        writer.write(json.dumps(value).encode() + b'\n')
        await writer.drain()
        writer.close()
        await writer.wait_closed()
    inspector = await asyncio.start_unix_server(inspect, '/evidence/inspect.sock')
    port = model_server.sockets[0].getsockname()[1]
    Path('/evidence/ready.json').write_text(json.dumps({'base_url': f'http://127.0.0.1:{port}/v1'}))
    try:
        await asyncio.wait_for(finished.wait(), 10800)
        print(json.dumps({'requests': len(requests), 'terminalMediated': False}))
    finally:
        if main_process is not None and main_process.returncode is None:
            main_process.terminate()
            await main_process.wait()
        if process.returncode is None:
            process.terminate()
            await process.wait()
        inspector.close()
        model_server.close()
        await inspector.wait_closed()
        await model_server.wait_closed()
        await asyncio.to_thread(ops.tmux, cfg, 'kill-server')


if __name__ == '__main__':
    asyncio.run(worker() if '--worker' in sys.argv else controller())
