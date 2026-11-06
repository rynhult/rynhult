"""PTY attach: `tmux attach` for one ACC session, streamed to accd over the broker socket.

After a JSON handshake line the connection carries frames in both directions:
    1 byte type + 4 byte big-endian length + payload
    b"d" data (terminal bytes)   b"r" resize (JSON {"rows", "cols"})   b"x" exit
    b"w" writer-only Codex history scroll (JSON signed line count)
Broker -> server b"g" reports an observer's current program geometry.
"""
from __future__ import annotations

import asyncio
import errno
import fcntl
import json
import logging
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

log = logging.getLogger(__name__)


class AttachmentScroll:
    """One coalesced wheel worker per attachment; input fences outstanding scrolls."""

    def __init__(self, config, tmux_name, enabled):
        self.config, self.tmux_name, self.enabled = config, tmux_name, enabled
        self.pending = 0
        self.scrolled = False
        self.task = None
        self.next_scroll = 0.0

    def enqueue(self, lines):
        if not self.enabled or type(lines) is not int or not 1 <= abs(lines) <= 200:
            return
        self.pending = max(-200, min(200, self.pending + lines))
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run())

    async def _run(self):
        loop = asyncio.get_running_loop()
        while self.pending:
            await asyncio.sleep(max(0, self.next_scroll - loop.time()))
            lines, self.pending = self.pending, 0
            if not lines:
                break
            # Even a partially failed scroll may have entered copy mode.
            self.scrolled = True
            try:
                await asyncio.to_thread(ops.scroll_history, self.config, self.tmux_name, lines)
            except (ops.OpError, OSError, subprocess.SubprocessError):
                log.warning("Terminal wheel scroll failed for %s", self.tmux_name, exc_info=True)
            self.next_scroll = loop.time() + 1 / 30

    async def before_input(self):
        self.pending = 0
        if self.task is not None:
            await self.task
        if not self.enabled or not self.scrolled:
            return True
        try:
            await asyncio.to_thread(ops.cancel_copy_mode, self.config, self.tmux_name)
        except (ops.OpError, OSError, subprocess.SubprocessError):
            log.warning("Terminal copy-mode cancel failed for %s", self.tmux_name, exc_info=True)
            # Retry cancellation on the next input if the pane may still be in mode.
            return False
        self.scrolled = False
        return True

    async def close(self):
        self.pending = 0
        if self.task is not None:
            await self.task


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


def observer_geometry(config, tmux_name, *, pin=True):
    """Pin the program window, not a read-only client's independently sized PTY.

    A typed preflight lets a new main refuse an old broker BEFORE attaching.
    tmux ignore-size alone is insufficient when every attached client is read-only.
    """
    if not isinstance(tmux_name, str) or not ops.TMUX_NAME.fullmatch(tmux_name):
        raise ops.OpError('Invalid session terminal name.')
    target = f'={tmux_name}:'
    if pin:
        ops.tmux(config, 'set-window-option', '-t', target, 'window-size', 'manual', check=True)
    result = ops.tmux(config, 'display-message', '-p', '-t', target, '#{pane_height} #{pane_width}', check=True)
    try:
        rows, cols = map(int, result.stdout.split())
        if not 5 <= rows <= 500 or not 20 <= cols <= 1000:
            raise ValueError()
    except (ValueError, AttributeError):
        raise ops.OpError('Observer geometry unavailable.') from None
    return {'geometry': 'manual-v1', 'rows': rows, 'cols': cols}


def resize_program(config, tmux_name, rows, cols):
    rows, cols = max(5, min(int(rows), 500)), max(20, min(int(cols), 1000))
    ops.tmux(config, 'resize-window', '-t', f'={tmux_name}:', '-x', str(cols), '-y', str(rows), check=True)


async def attach(config: BrokerConfig, params: dict, reader: asyncio.StreamReader,
                 writer: asyncio.StreamWriter) -> None:
    tmux_name = params.get("tmux_name", "")
    if params.get('automated'):
        from acc.providers.claude_auth import CODE, DOCS, MESSAGE, refusal

        from .claude_auth import refresh
        auth = await asyncio.to_thread(refresh, config, tmux_name)
        if refusal(auth['provider'], auth['auth_mode']):
            writer.write(json.dumps({'ok': False, 'error': MESSAGE, 'code': CODE, 'docs': DOCS}).encode() + b'\n')
            await writer.drain()
            return
    if not ops.TMUX_NAME.match(tmux_name) or ops.tmux(config, "has-session", "-t", f"={tmux_name}").returncode:
        writer.write(json.dumps({"ok": False, "error": "no such session"}).encode() + b"\n")
        await writer.drain()
        return
    wheel = ops.tmux(config, "show-options", "-v", "-t", tmux_name, "@acc-wheel").stdout.strip() == "on"
    scroll = AttachmentScroll(config, tmux_name, wheel)
    readonly = bool(params.get("readonly", True))
    fenced = params.get('fenced_control') is True
    fd, slave = os.openpty()
    loop = asyncio.get_running_loop()
    process = None
    out_task = reader_task = geometry_task = None

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

    # Observers follow the program, never their independently measured browser
    # viewport. Keep tmux queries off the stream event loop and send only changes.
    async def follow_geometry():
        nonlocal geometry
        failures = 0
        while True:
            await asyncio.sleep(min(.25 * 2 ** failures, 4))
            if not readonly:
                continue
            try:
                current = await asyncio.to_thread(observer_geometry, config, tmux_name, pin=False)
            except (ops.OpError, OSError, subprocess.SubprocessError):
                failures += 1
                log.warning("Terminal observer geometry failed for %s (attempt %s/5)",
                            tmux_name, failures, exc_info=True)
                if failures >= 5:
                    raise ops.OpError('Observer geometry repeatedly unavailable.') from None
                continue
            failures = 0
            if not readonly:
                continue
            if current != geometry:
                geometry = current
                set_size(fd, current['rows'], current['cols'])
                writer.write(frame(b'g', json.dumps(current).encode()))
                await writer.drain()

    try:
        # The handshake permits immediate input, including Ctrl-U. Do not leave
        # a cooked PTY window before tmux starts: its line discipline would eat
        # clearing keys locally and the later paste would append to old input.
        tty.setraw(slave, termios.TCSANOW)
        geometry = observer_geometry(config, tmux_name) if readonly else {}
        if not readonly:
            resize_program(config, tmux_name, params.get('rows', 40), params.get('cols', 120))
        set_size(fd, geometry.get("rows", params.get("rows", 40)), geometry.get("cols", params.get("cols", 120)))
        client_tty = os.ttyname(slave)
        args = module_command("acc.broker.pty_exec", "tmux", "-L", config.tmux_socket,
                              "attach-session", "-t", f"={tmux_name}")
        if readonly:
            args.append("-r")
        process = subprocess.Popen(args, stdin=slave, stdout=slave, stderr=slave, close_fds=True,
                                   start_new_session=True, env={**os.environ, "TERM": "xterm-256color"})
        os.close(slave)
        slave = None
        writer.write(json.dumps({"ok": True, "readonly": readonly, "wheel": wheel, **geometry, **({'fenced_control': True} if fenced else {})}).encode() + b"\n")
        await writer.drain()
        out_task = asyncio.create_task(pump_out())
        geometry_task = asyncio.create_task(follow_geometry())
        while True:
            reader_task = asyncio.create_task(read_frame(reader))
            done, _ = await asyncio.wait({reader_task, out_task, geometry_task}, return_when=asyncio.FIRST_COMPLETED)
            if geometry_task in done:
                geometry_task.result()
                break
            if out_task in done:
                reader_task.cancel()
                out_task.result()
                break
            kind, payload = reader_task.result()
            if kind == b"d" and not readonly:
                if await scroll.before_input():
                    os.write(fd, payload)
            elif kind == b"w" and not readonly and wheel:
                lines = json.loads(payload)
                if type(lines) is int and 1 <= abs(lines) <= 200:
                    scroll.enqueue(lines)
            elif kind == b'm' and fenced:
                change = json.loads(payload)
                if not isinstance(change, dict) or type(change.get('write')) is not bool:
                    raise ValueError('invalid control frame')
                await scroll.before_input()
                desired = not change['write']
                # The initial handshake can precede tmux's first redraw. Bound readiness retries.
                if desired != readonly:
                    for attempt in range(20):
                        # switch-client toggles both read-only and ignore-size. refresh-client -f
                        # can succeed without changing read-only, so a zero
                        # exit status from that command is not a control acknowledgement.
                        # Without -t, switch-client may select another session on this
                        # tmux server. Changing authority must never change the target.
                        result = ops.tmux(config, 'switch-client', '-c', client_tty, '-t', f'={tmux_name}', '-r')
                        if result.returncode == 0:
                            readonly = desired
                            break
                        await asyncio.sleep(.05)
                writer.write(frame(b'm', json.dumps({'write': not readonly}).encode()))
                await writer.drain()
            elif kind == b"r" and (not fenced or not readonly):
                size = json.loads(payload)
                set_size(fd, size["rows"], size["cols"])
                if not readonly:
                    resize_program(config, tmux_name, size['rows'], size['cols'])
            elif kind == b"x":
                break
    except (asyncio.IncompleteReadError, ConnectionError, ValueError, ops.OpError):
        pass
    finally:
        await scroll.close()
        loop.remove_reader(fd)
        tasks = [task for task in (out_task, reader_task, geometry_task) if task is not None]
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
