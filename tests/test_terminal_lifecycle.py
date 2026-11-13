"""Terminal failures must release control, PTYs, children and output readers."""
import asyncio
import os
import subprocess
import sys
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from acc.broker import attach as pty
from acc.server.broker_client import BrokerUnavailable
from acc.server.tty import Writers, bridge


async def test_unavailable_broker_releases_writer():
    writers = Writers()
    ws = object()

    async def unavailable():
        raise BrokerUnavailable("offline")

    with pytest.raises(BrokerUnavailable):
        await bridge(ws, unavailable, "session", "acc-test", writers, True, False, 40, 120)
    replacement = object()
    assert writers.claim("session", replacement, False) == (True, None)


async def test_cancelled_connect_releases_writer():
    writers = Writers()
    connecting = asyncio.Event()

    async def connect():
        connecting.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(bridge(object(), connect, "session", "acc-test", writers, True, False, 40, 120))
    await connecting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert writers.claim("session", object(), False) == (True, None)


@pytest.mark.parametrize("failure", ["size", "spawn", "handshake", "cancel"])
async def test_attach_cleans_up_every_setup_failure(monkeypatch, failure):
    master, slave = os.openpty()
    monkeypatch.setattr(pty.os, "openpty", lambda: (master, slave))
    monkeypatch.setattr(pty.ops, "tmux", lambda *a, **kw: SimpleNamespace(returncode=0, stdout='40 120'))
    process = None

    def spawn(*a, **kw):
        nonlocal process
        if failure == "spawn":
            raise FileNotFoundError("tmux")
        process = subprocess.Popen(["sleep", "30"])
        return process

    monkeypatch.setattr(pty.subprocess, "Popen", spawn if failure == "spawn" else subprocess.Popen)
    if failure == "size":
        monkeypatch.setattr(pty, "set_size", lambda *a: (_ for _ in ()).throw(OSError("size")))
    if failure in ("handshake", "cancel"):
        # An inert child stands in for tmux; allocation/handshake/cleanup remain real.
        original = subprocess.Popen

        def child(*a, **kw):
            nonlocal process
            process = original(["sleep", "30"])
            return process

        monkeypatch.setattr(pty.subprocess, "Popen", child)

    handshaking = asyncio.Event()

    class Writer:
        def write(self, data):
            pass

        async def drain(self):
            if failure == "cancel":
                handshaking.set()
                await asyncio.Event().wait()
            raise ConnectionError("handshake failed")

    try:
        attachment = pty.attach(SimpleNamespace(tmux_socket="test"), {"tmux_name": "acc-test"},
                                asyncio.StreamReader(), Writer())
        if failure == "cancel":
            task = asyncio.create_task(attachment)
            await asyncio.wait_for(handshaking.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(OSError) if failure != "handshake" else nullcontext():
                await attachment
        for fd in (master, slave):
            with pytest.raises(OSError):
                os.fstat(fd)
        if process is not None:
            assert process.poll() is not None
    finally:
        for fd in (master, slave):
            try:
                os.close(fd)
            except OSError:
                pass
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait()


async def test_stalled_output_stops_reading_pty(monkeypatch):
    master, slave = os.openpty()
    original_spawn = subprocess.Popen
    monkeypatch.setattr(pty.os, "openpty", lambda: (master, slave))
    monkeypatch.setattr(pty.ops, "tmux", lambda *a, **kw: SimpleNamespace(returncode=0, stdout='40 120'))
    monkeypatch.setattr(pty.subprocess, "Popen", lambda *a, **kw: original_spawn(["sleep", "30"]))
    original_read = os.read
    reads = 0

    def read(fd, size):
        nonlocal reads
        if fd == master:
            reads += 1
            return b"x" * 65536
        return original_read(fd, size)

    monkeypatch.setattr(pty.os, "read", read)
    blocked = asyncio.Event()

    class Writer:
        calls = 0

        def write(self, data):
            pass

        async def drain(self):
            self.calls += 1
            if self.calls > 1:
                blocked.set()
                await asyncio.Event().wait()

    # Keep the PTY readable throughout the stalled drain.
    os.write(slave, b"output\n")
    task = asyncio.create_task(pty.attach(SimpleNamespace(tmux_socket="test"), {"tmux_name": "acc-test"},
                                          asyncio.StreamReader(), Writer()))
    try:
        await asyncio.wait_for(blocked.wait(), 2)
        before = reads
        for _ in range(100):
            await asyncio.sleep(0)
        assert reads == before
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("readonly, expected_size", [(True, b"(40, 120)"), (False, b"(31, 97)")])
async def test_attach_acquires_controlling_terminal_without_setsid_executable(tmp_path, monkeypatch, readonly, expected_size):
    monkeypatch.setattr(pty.ops, "tmux", lambda *a, **kw: SimpleNamespace(returncode=0, stdout='40 120'))
    tmux = tmp_path / "tmux"
    tmux.write_text(f"#!{sys.executable}\n"
                    "import os, fcntl, termios, struct\n"
                    "assert not termios.tcgetattr(0)[3] & (termios.ICANON | termios.ECHO), 'attach must start raw'\n"
                    "os.close(os.open('/dev/tty', os.O_RDWR))\n"
                    "size = struct.unpack('HHHH', fcntl.ioctl(0, termios.TIOCGWINSZ, bytes(8)))\n"
                    "print('controlling tty', os.getsid(0) == os.getpid(), size[:2], flush=True)\n")
    tmux.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))  # no Linux setsid executable
    data = bytearray()

    class Writer:
        def write(self, chunk):
            data.extend(chunk)

        async def drain(self):
            pass

    await asyncio.wait_for(pty.attach(SimpleNamespace(tmux_socket="test"),
                                     {"tmux_name": "acc-test", "readonly": readonly, "rows": 31, "cols": 97},
                                     asyncio.StreamReader(), Writer()), 5)
    assert b"controlling tty True " + expected_size in data


async def test_observer_output_authorization_is_bounded_not_per_frame():
    from acc.server.tty import _frame
    reader = asyncio.StreamReader()
    reader.feed_data(b'{"ok":true,"readonly":true}\n' + _frame(b'd', b'output') * 3000 + _frame(b'x', b''))
    checks, seen = 0, 0
    async def authorize():
        nonlocal checks
        checks += 1
        await asyncio.sleep(0)
        return True
    class Socket:
        async def send_text(self, data):
            pass
        async def send_bytes(self, data):
            nonlocal seen
            seen += 1
        async def receive_text(self):
            await asyncio.Event().wait()
    class Stream:
        def write(self, data):
            pass
        async def drain(self):
            pass
        def close(self):
            pass
    async def connect():
        return reader, Stream()
    await bridge(Socket(), connect, 'session', 'acc-test', Writers(), False, False, 40, 120,
                 signed_in=authorize, bound_run='1', bound_instance='test')
    assert seen == 3000
    assert checks < 100  # not 3000 synchronous DB checks, even on a loaded host


@pytest.mark.parametrize('data,human', [
    ('', False), ('\x1b[I', False), ('\x1b[O', False),
    ('\x1b[?1;2c', False), ('\x1b[>0;276;0c', False),
    ('\x1b[12;34R', False), ('\x1b[?12;34R', False),
    ('\x1b[I\x1b[?1;2c\x1b[12;34R', False),
    ('\x1b[Ihello', True), ('x', True), ('\r', True),
    ('\x03', True), ('\x1b', True), ('\x1b[A', True),
    ('\x1b[200~paste\x1b[201~', True),
    ('\x1b[1;2R', True), ('\x1b[1;3R', True), ('\x1b[1;5R', True),
    ('\x1b[1;8R', True), ('\x1b[1;1R', True),
    ('\x1b[0;0R', True), ('\x1b[;c', True),
])
async def test_bridge_cancels_only_human_input_before_forwarding_exact_bytes(data, human):
    import json

    from fastapi import WebSocketDisconnect

    from acc.server.tty import _frame

    entered, release, forwarded = asyncio.Event(), asyncio.Event(), asyncio.Event()
    chunks = []
    reader = asyncio.StreamReader()
    reader.feed_data(b'{"id":1,"ok":true,"readonly":false}\n')

    class Socket:
        delivered = False

        async def send_text(self, _data):
            pass

        async def receive_text(self):
            if not self.delivered:
                self.delivered = True
                return json.dumps({'t': 'i', 'd': data})
            await forwarded.wait()
            raise WebSocketDisconnect()

    class Writer:
        def write(self, chunk):
            chunks.append(chunk)
            if chunk == _frame(b'd', data.encode()):
                forwarded.set()

        async def drain(self):
            pass

        def close(self):
            pass

    async def connect():
        return reader, Writer()

    async def cancel():
        entered.set()
        await release.wait()

    task = asyncio.create_task(bridge(Socket(), connect, 'session', 'acc-test', Writers(),
                                      True, False, 40, 120, on_input=cancel))
    try:
        if human:
            await asyncio.wait_for(entered.wait(), 1)
            assert not forwarded.is_set()
            release.set()
        await asyncio.wait_for(task, 1)
        assert entered.is_set() == human
        assert chunks.count(_frame(b'd', data.encode())) == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_relay_terminal_preserves_bytes_and_refuses_unproven_drain():
    from acc.session_relay.adapters.base import InputGrant, TerminalRun
    from acc.session_relay.protocol import Actor, ActorKind, Channel, Fence, RelayError
    from acc.session_relay.terminal_host import TmuxTerminalHost
    run = TerminalRun('session1', 'run1')
    grant = InputGrant(run, 'attachment1', Actor(ActorKind.PERSON, 'person1', Channel.TERMINAL),
                       'fixture-grant', 1, Fence(1, 'broker1', 'hub1'))
    written = []
    class Writer:
        def write(self, data):
            written.append(data)
        async def drain(self):
            pass
        def close(self):
            pass
        async def wait_closed(self):
            pass
    async def opener(run):
        return asyncio.StreamReader(), Writer()
    current = [True]
    async def valid(grant):
        return current[0]
    host = TmuxTerminalHost(run, opener, valid, valid)
    await host.claim_input(grant)
    data = b'\x1b[200~hello\r\n\xc3\xa5\x1b[201~\x1b[D'
    result = await host.write_frames(grant, 1, data)
    assert written == [pty.frame(b'd', data)]
    assert result.watermark.admitted == 1 and result.watermark.processed == 0
    with pytest.raises(RelayError):
        await host.drain(grant, 1)
    with pytest.raises(RelayError):
        await host.write_frames(grant, 1, data)
    current[0] = False
    with pytest.raises(RelayError):
        await host.write_frames(grant, 2, data)
    assert len(written) == 1
    await host.detach(run)


async def test_relay_unknown_frame_write_blocks_replay_and_takeover():
    from dataclasses import replace

    from acc.session_relay.adapters.base import InputGrant, TerminalRun
    from acc.session_relay.protocol import Actor, ActorKind, Channel, Fence, RelayError
    from acc.session_relay.terminal_host import TmuxTerminalHost
    run = TerminalRun('session1', 'run1')
    grant = InputGrant(run, 'attachment1', Actor(ActorKind.PERSON, 'person1', Channel.TERMINAL),
                       'fixture-grant', 1, Fence(1, 'broker1', 'hub1'))
    class Writer:
        def write(self, data):
            pass
        async def drain(self):
            raise ConnectionError('partial write')
    async def opener(run):
        return asyncio.StreamReader(), Writer()
    async def valid(grant):
        return True
    host = TmuxTerminalHost(run, opener, valid, valid)
    await host.claim_input(grant)
    assert (await host.write_frames(grant, 1, b'hello')).partial
    with pytest.raises(RelayError):
        await host.write_frames(grant, 2, b'hello')
    with pytest.raises(RelayError):
        await host.claim_input(replace(grant, epoch=2))


async def test_relay_stop_and_unmediated_terminal_paths_with_real_fixture_broker(api, project):
    from helpers import wait_for, write_db

    from acc.server import security_settings
    from acc.server.models import RelaySession, Session
    from acc.server.session_relay import store

    created = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'codex', 'name': 'Relay boundary'})
    assert created.status_code == 201, created.text
    identifier = created.json()['id']
    async def verified():
        with api.app.state.db() as db:
            session = db.get(Session, identifier)
            return bool(session.runs and session.runs[-1].verified_at and session.native_id)
    await wait_for(verified)
    with write_db(api.app.state.db) as db:
        session = db.get(Session, identifier)
        store.enroll(db, identifier, session.runs[-1].id, 'fixture-binding', identity={'fixture': True},
                     frozen_policy={}, policy_revision=security_settings.revision(db))
        db.commit()
    detail = await api.get(f'/api/sessions/{identifier}')
    assert detail.json()['session_relay'] is True
    listed = (await api.get('/api/sessions')).json()
    assert next(row for row in listed if row['id'] == identifier)['session_relay'] is True
    assert (await api.get(f'/api/sessions/{identifier}/terminal-reference')).status_code == 409
    supervisor = api.app.state.supervisor
    assert (await api.post(f'/api/sessions/{identifier}/message', json={'text': 'Must not type'})).status_code == 409
    assert (await api.post(f'/api/sessions/{identifier}/interrupt')).status_code == 409
    await supervisor.stop(identifier, 'supervisor', for_sleep=True)
    with api.app.state.db() as db:
        assert not db.get(RelaySession, identifier).sealed
        assert db.get(Session, identifier).desired_state == 'running'
    assert (await api.post(f'/api/sessions/{identifier}/stop')).status_code == 200
    with api.app.state.db() as db:
        assert db.get(RelaySession, identifier).sealed
        assert db.get(Session, identifier).desired_state == 'stopped'
