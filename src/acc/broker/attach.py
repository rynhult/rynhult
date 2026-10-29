"""PTY attach: `tmux attach` for one ACC session, streamed to accd over the broker socket.

After a JSON handshake line the connection carries frames in both directions:
    1 byte type + 4 byte big-endian length + payload
    b"d" data (terminal bytes)   b"r" resize (JSON {"rows", "cols"})   b"x" exit
"""
from __future__ import annotations

import asyncio
import errno
import fcntl
import json
import os
import signal
import struct
import subprocess
import termios
import tty

from acc.python_child import module_command

from . import ops
from .config import BrokerConfig

HEADER = struct.Struct(">cI")
MAX_FRAME = 1 << 20


async def read_frame(reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
    kind, length = HEADER.unpack(await reader.readexactly(HEADER.size))
    if length > MAX_FRAME:
        raise ValueError("frame too large")
    return kind, await reader.readexactly(length)


def frame(kind: bytes, payload: bytes) -> bytes:
    return HEADER.pack(kind, len(payload)) + payload


def set_size(fd: int, rows: int, cols: int) -> None:
    rows, cols = max(5, min(int(rows), 500)), max(20, min(int(cols), 1000))
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


async def attach(config: BrokerConfig, params: dict, reader: asyncio.StreamReader,
                 writer: asyncio.StreamWriter) -> None:
    tmux_name = params.get("tmux_name", "")
    if not ops.TMUX_NAME.match(tmux_name) or ops.tmux(config, "has-session", "-t", f"={tmux_name}").returncode:
        writer.write(json.dumps({"ok": False, "error": "no such session"}).encode() + b"\n")
        await writer.drain()
        return
    readonly = bool(params.get("readonly", True))
    fd, slave = os.openpty()
    loop = asyncio.get_running_loop()
    process = None
    out_task = reader_task = None

    async def pump_out() -> None:
        while True:
            ready = loop.create_future()

            def readable() -> None:
                loop.remove_reader(fd)
                if not ready.done():
                    ready.set_result(None)

            loop.add_reader(fd, readable)
            try:
                await ready
            finally:
                loop.remove_reader(fd)
            try:
                chunk = os.read(fd, 65536)
            except OSError as error:
                if error.errno != errno.EIO:
                    raise
                chunk = b""  # PTY EOF on Linux
            if not chunk:
                break
            writer.write(frame(b"d", chunk))
            # No reader is registered while drain is blocked: backpressure reaches the PTY.
            await writer.drain()
        writer.write(frame(b"x", b""))
        await writer.drain()

    try:
        # The handshake permits immediate input, including Ctrl-U. Do not leave
        # a cooked PTY window before tmux starts: its line discipline would eat
        # clearing keys locally and the later paste would append to old input.
        tty.setraw(slave, termios.TCSANOW)
        set_size(fd, params.get("rows", 40), params.get("cols", 120))
        args = module_command("acc.broker.pty_exec", "tmux", "-L", config.tmux_socket,
                              "attach-session", "-t", f"={tmux_name}")
        if readonly:
            args.append("-r")
        process = subprocess.Popen(args, stdin=slave, stdout=slave, stderr=slave, close_fds=True,
                                   start_new_session=True, env={**os.environ, "TERM": "xterm-256color"})
        os.close(slave)
        slave = None
        writer.write(json.dumps({"ok": True, "readonly": readonly}).encode() + b"\n")
        await writer.drain()
        out_task = asyncio.create_task(pump_out())
        while True:
            reader_task = asyncio.create_task(read_frame(reader))
            done, _ = await asyncio.wait({reader_task, out_task}, return_when=asyncio.FIRST_COMPLETED)
            if out_task in done:
                reader_task.cancel()
                out_task.result()
                break
            kind, payload = reader_task.result()
            if kind == b"d" and not readonly:
                os.write(fd, payload)
            elif kind == b"r":
                size = json.loads(payload)
                set_size(fd, size["rows"], size["cols"])
            elif kind == b"x":
                break
    except (asyncio.IncompleteReadError, ConnectionError, ValueError):
        pass
    finally:
        loop.remove_reader(fd)
        tasks = [task for task in (out_task, reader_task) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if slave is not None:
            os.close(slave)
        os.close(fd)
        if process is not None:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)  # detaches this client, not the tmux session
            await asyncio.to_thread(process.wait)
