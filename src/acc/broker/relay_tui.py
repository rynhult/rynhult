"""Owned read-only-placement Codex client; never another execution owner.

Only reviewed launch composes this view. Its private home has no account keys;
namespace placement exposes the exact Unix socket and read-only workspace/config.
All input still needs the independent main grant and strict Host retirement.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import select
import signal
import socket
from pathlib import Path
from types import SimpleNamespace

from acc import peercred
from acc.session_relay import protocol as p

from . import ops
from .attach import attach
from .relay_enclosure import Enclosure, incarnation
from .session_relay import private_dir


class OwnedTUI:
    def __init__(self, root, config, spec, executable, native_socket, *, digest):
        self.root, self.config, self.spec = root, config, spec
        self.executable = Path(executable).resolve(strict=True)
        self.native_socket, self.digest = native_socket, digest
        self.name = 'acc-relay-view-' + hashlib.sha256((spec.spec['session_id'] + ':' + str(spec.run_id) + ':' + str(root)).encode()).hexdigest()[:24]
        self.enclosure = None
        self.started = False
        self.closed = False
        self.identity = None
        self.peer = None
        self.peer_fd = None
        self.attachments = set()
        self.detachers = set()
        self.exit_proven = False

    @staticmethod
    def available():
        return os.name == 'posix' and Path('/proc/self/stat').exists() and Path('/usr/bin/bwrap').is_file()

    def _pin(self):
        if not self.available() or p.sha256_digest(self.executable.read_bytes()) != self.digest:
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        self.native_socket.validate()
        workspace = Path(self.spec.binding.workspace)
        if not workspace.is_absolute() or workspace.resolve(strict=True) != workspace or not workspace.is_dir():
            raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
        return workspace

    async def start(self, authorize):
        if self.enclosure is not None or self.started or self.closed:
            raise p.RelayError(p.ErrorCode.BUSY)
        workspace = self._pin()
        if await authorize() is not True:
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        os.close(private_dir(self.root))
        home = self.root / 'home'
        os.close(private_dir(home))
        os.close(private_dir(home / '.codex'))
        config_file = home / '.codex/config.toml'
        fd = os.open(config_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
        try:
            os.write(fd, b'# Owned presentation only; execution policy is server-held.\n'
                         b'# Host validates every complete ASCII frame; Enter is always a separate frame.\n'
                         b'disable_paste_burst = true\n')
            os.fsync(fd)
        finally:
            os.close(fd)
        self.enclosure = Enclosure(self.root, SimpleNamespace(run_id=str(self.spec.run_id) + '-' + self.name), '/usr/bin/bwrap',
            lifetime={'pid': os.getpid(), 'started': incarnation(os.getpid())})
        args = ['--unshare-all', '--die-with-parent', '--cap-drop', 'ALL']
        for system in ('/usr', '/bin', '/lib', '/lib64'):
            if Path(system).exists():
                args += ['--ro-bind', system, system]
        args += ['--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp', '--tmpfs', '/home',
                 '--dir', '/home/relay', '--bind', str(home), '/home/relay',
                 '--ro-bind', str(config_file), '/home/relay/.codex/config.toml',
                 '--ro-bind', str(workspace), str(workspace),
                 '--ro-bind', str(self.native_socket.path), '/relay-native.sock',
                 '--clearenv', '--setenv', 'PATH', '/usr/bin:/bin', '--setenv', 'HOME', '/home/relay',
                 '--setenv', 'CODEX_HOME', '/home/relay/.codex', '--setenv', 'TERM', 'xterm-256color',
                 '--setenv', 'LANG', 'C.UTF-8', '--chdir', '/home/relay', '--', str(self.executable),
                 '--remote', 'unix:///relay-native.sock', '--no-alt-screen', 'resume', self.native_socket.native_id]
        # env -i clears tmux's inherited account environment before the supervisor
        # starts. The inner namespace also independently clears its environment.
        if await authorize() is not True:
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        if (await asyncio.to_thread(ops.tmux, self.config, 'has-session', '-t', '=' + self.name)).returncode == 0:
            raise p.RelayError(p.ErrorCode.BUSY)
        await asyncio.to_thread(ops.tmux, self.config, 'new-session', '-d', '-s', self.name, '-x', '120', '-y', '32',
            '/usr/bin/env', '-i', 'PATH=/usr/bin:/bin', 'TERM=xterm-256color', str(self.enclosure.cli), *args, check=True)
        self.started = True
        value = await asyncio.to_thread(ops.tmux, self.config, 'display-message', '-p', '-t', self.name + ':0.0',
                                        '#{session_id} #{pane_pid} #{pid}', check=True)
        fields = value.stdout.strip().split()
        if len(fields) != 3:
            raise RuntimeError('Owned tmux identity unavailable: ' + repr(value.stdout))
        session, pane, server = fields
        self.identity = session, int(pane), int(server), incarnation(int(server))
        # Require the verified native connection, not merely a tmux spawn.
        # One launch only: allow a bounded cold bootstrap under concurrent load.
        # A connected socket alone is never readiness or permission to write.
        for _ in range(750):
            if self.bootstrapped():
                break
            await asyncio.sleep(.02)
        if not self.bootstrapped():
            await self.close()
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        self._pin()
        if await authorize() is not True:
            await self.close()
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        return self

    def bootstrapped(self):
        return self.peer is not None and any(self.accepts(client) for client in tuple(self.native_socket.bootstrapped_clients))

    def alive(self):
        # A reader may reconnect without replacing its already-pinned native
        # process. This does not qualify a never-bootstrapped view or input.
        return self.peer is not None and self.peer_fd is not None and self._accepts_pid(self.peer[0])

    def accepts(self, websocket):
        if self.closed or self.enclosure is None:
            return False
        sock = websocket.transport.get_extra_info('socket')
        if peercred.peer_uid(sock) != os.getuid():
            return False
        pid = peercred.UCRED.unpack(sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, peercred.UCRED.size))[0]
        return self._accepts_pid(pid)

    def _accepts_pid(self, pid):
        if self.closed or self.enclosure is None:
            return False
        try:
            started = json.loads((self.enclosure.path / 'started.json').read_text())
            identity = f"{started['pid']}:{started['started']}"
            if not self.enclosure.proof('started.json', identity) or incarnation(started['pid']) != started['started']:
                return False
            if Path(f'/proc/{pid}/exe').resolve(strict=True) != self.executable:
                return False
            current = pid
            for _ in range(12):
                if current == started['pid']:
                    break
                current = int(Path(f'/proc/{current}/stat').read_text().rsplit(')', 1)[1].split()[1])
            else:
                return False
            now = pid, incarnation(pid)
            if self.peer is not None:
                poll = select.poll()
                poll.register(self.peer_fd, select.POLLIN)
                return now == self.peer and not poll.poll(0)
            descriptor = os.pidfd_open(pid)
            if incarnation(pid) != now[1]:
                os.close(descriptor)
                return False
            self.peer, self.peer_fd = now, descriptor
            return True
        except (OSError, ValueError, KeyError):
            return False

    async def capture(self, run):
        def current():
            if self.closed or not self.started or self.identity is None or self.peer_fd is None:
                return False
            session, pane, server, tick = self.identity
            if incarnation(server) != tick or select.select([self.peer_fd], [], [], 0)[0]:
                return False
            value = ops.tmux(self.config, 'display-message', '-p', '-t', self.name + ':0.0',
                             '#{session_id} #{pane_pid} #{pid}')
            return value.returncode == 0 and value.stdout.strip() == f'{session} {pane} {server}'
        if (run.session_id != self.spec.spec['session_id'] or run.run_id != self.spec.run_id
                or not await asyncio.to_thread(current)):
            raise p.RelayError(p.ErrorCode.STALE_RUN)
        value = await asyncio.to_thread(ops.tmux, self.config, 'capture-pane', '-p', '-J', '-S', '-200',
                                       '-t', '=' + self.name + ':')
        if value.returncode or not await asyncio.to_thread(current):
            raise p.RelayError(p.ErrorCode.STALE_RUN)
        raw = value.stdout.encode()
        truncated = len(raw) > 128 << 10
        text = raw[-(128 << 10):].decode(errors='replace')
        if truncated:
            text = text.partition('\n')[2]
        return {'text': text, 'truncated': truncated}

    async def open(self, run):
        if self.closed or not self.started or run.session_id != self.spec.spec['session_id'] or run.run_id != self.spec.run_id:
            raise p.RelayError(p.ErrorCode.STALE_RUN)
        left, right = socket.socketpair()
        left.setblocking(False)
        right.setblocking(False)
        reader, writer = await asyncio.open_connection(sock=left)
        remote_reader, remote_writer = await asyncio.open_connection(sock=right)
        async def serve():
            try:
                await attach(self.config, {'tmux_name': self.name, 'readonly': False, 'rows': 32, 'cols': 120}, remote_reader, remote_writer)
            finally:
                remote_writer.close()
                self.attachments.discard(writer)
        task = asyncio.create_task(serve())
        self.detachers.add(task)
        task.add_done_callback(self.detachers.discard)
        self.attachments.add(writer)
        try:
            response = json.loads(await asyncio.wait_for(reader.readline(), 5))
            if response.get('ok') is not True or self.closed:
                raise p.RelayError(p.ErrorCode.UNSUPPORTED)
            return reader, writer
        except BaseException:
            writer.close()
            raise

    async def close(self):
        if self.closed:
            return self.exit_proven
        self.closed = True
        for writer in tuple(self.attachments):
            writer.close()
        pending = tuple(self.detachers)
        if pending:
            _, unfinished = await asyncio.wait(pending, timeout=5)
            for task in unfinished:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        try:
            if self.enclosure:
                for _ in range(100):
                    record = self.enclosure.path / 'started.json'
                    if record.exists():
                        break
                    await asyncio.sleep(.02)
                if record.exists():
                    started = json.loads(record.read_text())
                    identity = f"{started['pid']}:{started['started']}"
                    if not self.enclosure.proof('started.json', identity):
                        return False
                    if not self.enclosure.proof('exit.json', identity):
                        try:
                            descriptor = os.pidfd_open(started['pid'])
                            try:
                                if incarnation(started['pid']) == started['started']:
                                    signal.pidfd_send_signal(descriptor, signal.SIGTERM)
                            finally:
                                os.close(descriptor)
                        except ProcessLookupError:
                            pass
                    for _ in range(150):
                        if self.enclosure.proof('exit.json', identity):
                            self.exit_proven = True
                            break
                        await asyncio.sleep(.02)
            if self.started and self.identity:
                session, pane, server, tick = self.identity
                try:
                    same_server = incarnation(server) == tick
                except OSError:
                    same_server = False
                value = await asyncio.to_thread(ops.tmux, self.config, 'display-message', '-p', '-t', self.name + ':0.0',
                                                '#{session_id} #{pane_pid} #{pid}')
                if same_server and value.stdout.strip() == f'{session} {pane} {server}':
                    await asyncio.to_thread(ops.tmux, self.config, 'kill-session', '-t', '=' + self.name)
        finally:
            if self.peer_fd is not None:
                os.close(self.peer_fd)
                self.peer_fd = None
            if self.enclosure:
                self.enclosure.close()
        return self.exit_proven
