"""Production account broker/owned tmux inside the enforced offline fixture.

No authority overrides or synthetic Host callbacks; diagnostic wrapper only. The parent supplies only the
artificial main's normal broker RPCs. Inspection is private test evidence only.
"""
import asyncio
import json
import os
import pwd
import resource
import select
import threading
import time
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
    timings = {}
    timing_lock = threading.Lock()
    def timed(name, elapsed):
        with timing_lock:
            entry = timings.setdefault(name, {'count': 0, 'seconds': 0., 'max_seconds': 0.})
            entry['count'] += 1
            entry['seconds'] += elapsed
            entry['max_seconds'] = max(entry['max_seconds'], elapsed)
    def timing_snapshot():
        with timing_lock:
            return json.loads(json.dumps(timings))
    original_probe = claude_auth.probe
    def diagnostic_probe(*args, **kwargs):
        started = time.monotonic()
        try:
            result = original_probe(*args, **kwargs)
        finally:
            timed('auth.status', time.monotonic() - started)
        if result != 'api_key':
            with Path('/evidence/placement-errors.log').open('a') as stream:
                stream.write('Native auth classification: ' + result + '\n')
        return result
    claude_auth.probe = diagnostic_probe
    factory = RuntimeTransportFactory(config, owned_view_factory=None)
    # Test diagnostics only; preserve every production result/exception.
    import traceback

    from acc.broker.relay_ownership import ExclusiveRuntime
    original_handle = ExclusiveRuntime.handle
    async def diagnostic(self, method, params):
        started = time.monotonic()
        try:
            return await original_handle(self, method, params)
        except Exception:
            with Path('/evidence/placement-errors.log').open('a') as stream:
                stream.write(traceback.format_exc())
                for file in config.run_dir.rglob('started.json'):
                    record = json.loads(file.read_text())
                    stream.write(json.dumps({k: record.get(k) for k in ('pid', 'started', 'runId')}) + '\n')
                panes = ops.tmux(config, 'list-panes', '-a', '-F', '#{pane_pid}:#{pane_dead}:#{pane_current_command}')
                stream.write(panes.stdout)
                screen = ops.tmux(config, 'capture-pane', '-p', '-J', '-S', '-30', '-t', '=' + data['tmux_name'] + ':')
                stream.write(screen.stdout[-6000:])
            raise
        finally:
            timed('rpc.' + method, time.monotonic() - started)
    ExclusiveRuntime.handle = diagnostic
    retired, samples = [], []
    from acc.broker.claude_owner import ClaudeOwnerFactory
    original_prepare = ClaudeOwnerFactory.prepare
    async def tracked_prepare(self, spec):
        old = (self.hooks, self.enclosure, self.observer, self.input_attachment,
               getattr(self.current, 'process', None))
        result = await original_prepare(self, spec)
        hooks, enclosure, observer, attachment, process = old
        if hooks is not None:
            assert hooks.fd is None and not hooks.handlers and not hooks.pending
            assert enclosure.fd is None
            if observer is not None:
                assert observer.closed
            if attachment is not None:
                assert attachment.closed
            if process is not None and hasattr(process, 'fd'):
                assert process.fd is None
            retired.append({'hooks_closed': True, 'enclosure_closed': True})
        return result
    ClaudeOwnerFactory.prepare = tracked_prepare
    broker = Broker(config, relay_transport=factory)
    server = await asyncio.start_unix_server(broker.handle, str(config.socket_path))
    finish = asyncio.Event()
    errors, records = [], []
    background_fd = None
    def background_process(owner, reported):
        # The native sandbox adds a PID namespace. A PID written by the tool
        # is not necessarily a broker-visible PID; pin its exact owned process.
        root = owner.process.pid
        script = str(Path(data['workspace']) / '.relay-background.py').encode()
        matches = []
        for entry in Path('/proc').glob('[0-9]*'):
            try:
                args = (entry / 'cmdline').read_bytes().split(b'\0')
                if len(args) < 2 or args[1] != script or entry.stat().st_uid != os.getuid():
                    continue
                status = (entry / 'status').read_text()
                ns = next(line.split()[1:] for line in status.splitlines() if line.startswith('NSpid:'))
                if int(ns[-1]) != reported:
                    continue
                pid, seen = int(entry.name), set()
                current = pid
                while current != root and current > 1 and current not in seen:
                    seen.add(current)
                    current = int(Path(f'/proc/{current}/stat').read_text().rsplit(')', 1)[1].split()[1])
                if current == root:
                    matches.append(pid)
            except (OSError, ValueError, StopIteration):
                continue
        assert len(matches) <= 1, 'Ambiguous native background identity'
        return matches[0] if matches else None
    async def inspect(reader, writer):
        nonlocal background_fd
        try:
            method = json.loads(await asyncio.wait_for(reader.readline(), 3))['method']
            state = next(iter(factory.active.values()), None)
            owner = state.owners.current if state else None
            if method == 'state':
                host = getattr(owner, 'host', None)
                result = {'active': len(factory.active), 'view': state.controller.switch.view if state and state.controller else None,
                    'processed': getattr(host, 'processed', None), 'admitted': getattr(host, 'admitted', None),
                    'idle': getattr(owner, 'idle', None), 'failed': getattr(owner, 'failed', None),
                    'native_transcript': bool(state.native_transcript) if state else False}
            elif method == 'lose_application_evidence':
                original_call = state.peer.call
                async def lost_publication(method, params):
                    if method == 'publication' and params.get('kind') == 'decision.applied':
                        raise ConnectionError('Artificial loss of native application evidence before main')
                    return await original_call(method, params)
                state.peer.call = lost_publication
                result = {'applicationEvidenceLoss': True}
            elif method == 'background':
                path = (Path(data['workspace']) / '.relay-background.pid')
                if background_fd is None and path.exists():
                    pid = background_process(owner, int(path.read_text()))
                    if pid is not None:
                        background_fd = os.pidfd_open(pid)
                exited = background_fd is not None and bool(select.select([background_fd], [], [], 0)[0])
                result = {'running': background_fd is not None and not exited, 'exited': exited,
                          'background': list(getattr(owner, 'background', [])),
                          'active_turn': getattr(owner, 'active', None), 'active': len(factory.active),
                          'failure': getattr(owner, 'failure', None),
                          'native_tools': list(getattr(owner, 'native_tools', []))}
            elif method == 'release_background':
                (Path(data['workspace']) / '.relay-background.release').write_text('complete')
                result = {'released': True}
            elif method == 'sample':
                result = {'retired': len(retired), 'fds': len(list(Path('/proc/self/fd').iterdir())),
                          'tasks': len(asyncio.all_tasks()), 'active': len(factory.active),
                          'rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                          'hooks': len(state.owners.hooks.handlers) if state else 0,
                          'timings': timing_snapshot()}
                samples.append(result)
            elif method == 'screen':
                value = await asyncio.to_thread(ops.tmux, config, 'capture-pane', '-p', '-J', '-S', '-100', '-t', '=' + data['tmux_name'] + ':')
                result = {'text': value.stdout, 'returncode': value.returncode}
            elif method == 'finish':
                result = {'done': True}
                finish.set()
            else:
                raise ValueError('Unknown inspection method')
        except Exception as error:
            errors.append(repr(error))
            result = {'error': repr(error)}
        writer.write(json.dumps(result).encode() + b'\n')
        await writer.drain()
        writer.close()
        await writer.wait_closed()
    inspector = await asyncio.start_unix_server(inspect, '/evidence/owned-inspect.sock')
    Path('/evidence/owned-ready.json').write_text(json.dumps({'base_url': env['ANTHROPIC_BASE_URL']}))
    try:
        await asyncio.wait_for(finish.wait(), data.get('proof_budget', 1200))
        for _ in range(500):
            if not factory.active:
                break
            await asyncio.sleep(.02)
        assert not factory.active, 'Parent must close the authenticated production runtime'
        for file in config.run_dir.rglob('exit.json'):
            record = json.loads(file.read_text())
            records.append({'empty': record.get('empty'), 'runId': record.get('runId'), 'status': record.get('status')})
        assert records and all(record['empty'] for record in records)
    finally:
        if background_fd is not None:
            os.close(background_fd)
        server.close()
        inspector.close()
        await server.wait_closed()
        await inspector.wait_closed()
        await asyncio.to_thread(ops.tmux, config, 'kill-server')
    assert not errors
    return {'timings': timing_snapshot(), 'retired': retired, 'samples': samples, 'production_placement': True, 'production_opener': True, 'empty_tree_records': records,
            'terminalMediated': False, 'scope': 'actual production account broker/factory/SDK/tmux/Host/opaque grants; artificial main principal, no browser'}
