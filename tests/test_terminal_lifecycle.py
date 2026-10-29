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
    monkeypatch.setattr(pty.ops, "tmux", lambda *a: SimpleNamespace(returncode=0))
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
    monkeypatch.setattr(pty.ops, "tmux", lambda *a: SimpleNamespace(returncode=0))
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


async def test_attach_acquires_controlling_terminal_without_setsid_executable(tmp_path, monkeypatch):
    monkeypatch.setattr(pty.ops, "tmux", lambda *a: SimpleNamespace(returncode=0))
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
                                     {"tmux_name": "acc-test", "rows": 31, "cols": 97},
                                     asyncio.StreamReader(), Writer()), 5)
    assert b"controlling tty True (31, 97)" in data
