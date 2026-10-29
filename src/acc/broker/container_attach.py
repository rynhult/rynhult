"""A container session's terminal, in its tmux pane: relays this pane to the launcher unit's attach socket.

The container runs in that unit's private mount namespace and root-owned user namespace, which this account can't
enter, so `podman attach` here can't reach it. The execution guard's relay listens on
/run/acc-ct-attach/<digest>/attach.sock instead (docs/decisions/drafts/container-attach-path.md). Frames, both ways:
    1 byte type + 4 byte big-endian length + payload: b"d" data, b"r" resize (JSON {"rows", "cols"}), b"x" exit
Every key goes to the agent (there are no detach keys); when the container exits, so does the pane.

Anything running as the account could put its own socket at that path, so before a single key is sent the pane checks
that the listener runs in the session's unit (its cgroup or below): the account can't move a process in there.
"""
from __future__ import annotations

import fcntl
import json
import os
import selectors
import signal
import socket
import struct
import sys
import termios
import time
import tty

FRAME = struct.Struct(">cI")
MAX_FRAME = 1 << 16
CONNECT_WAIT = 30.0


def frame(kind: bytes, payload: bytes = b"") -> bytes:
    return FRAME.pack(kind, len(payload)) + payload


def connect(path: str, wait: float = CONNECT_WAIT) -> socket.socket:
    """The relay starts with the unit, just before the container: wait briefly for its socket."""
    deadline = time.monotonic() + wait
    while True:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            conn.connect(path)
            return conn
        except (FileNotFoundError, ConnectionRefusedError):
            conn.close()
            if time.monotonic() > deadline:
                raise
            time.sleep(0.2)


def inside(cgroup: str | None, unit: str) -> bool:
    return cgroup is not None and (cgroup == unit or cgroup.startswith(unit.rstrip("/") + "/"))


def cgroup_of(pid: int) -> str | None:
    try:
        lines = open(f"/proc/{pid}/cgroup").read().splitlines()
    except OSError:
        return None
    return next((line[3:] for line in lines if line.startswith("0::")), None)


def relay_is_the_units(conn: socket.socket, unit: str) -> bool:
    """The listener (SO_PEERCRED: who called listen) is this account, in the session's unit."""
    pid, uid, _ = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    return pid > 0 and uid == os.getuid() and inside(cgroup_of(pid), unit)


def size(fd: int) -> bytes | None:
    try:
        rows, cols, _, _ = struct.unpack("HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8))
    except OSError:
        return None
    return json.dumps({"rows": rows, "cols": cols}).encode() if rows and cols else None


def run(conn: socket.socket, stdin: int, stdout: int) -> int:
    """Relay until the container ends (0) or the relay goes away (1)."""
    wake_read, wake_write = os.pipe()
    os.set_blocking(wake_write, False)
    previous = signal.set_wakeup_fd(wake_write)
    signal.signal(signal.SIGWINCH, lambda *_: None)
    events = selectors.DefaultSelector()
    events.register(conn, selectors.EVENT_READ)
    events.register(stdin, selectors.EVENT_READ)
    events.register(wake_read, selectors.EVENT_READ)
    pending = b""
    try:
        if (current := size(stdin)) is not None:
            conn.sendall(frame(b"r", current))
        while True:
            for key, _ in events.select():
                if key.fileobj == wake_read:
                    os.read(wake_read, 512)
                    if (current := size(stdin)) is not None:
                        conn.sendall(frame(b"r", current))
                elif key.fileobj == stdin:
                    data = os.read(stdin, MAX_FRAME)
                    if not data:  # the pane's input closed: the container keeps running
                        return 0
                    conn.sendall(frame(b"d", data))
                else:
                    data = conn.recv(65536)
                    if not data:
                        return 1
                    pending += data
                    while len(pending) >= FRAME.size:
                        kind, length = FRAME.unpack_from(pending)
                        if length > MAX_FRAME:
                            return 1
                        if len(pending) < FRAME.size + length:
                            break
                        payload, pending = pending[FRAME.size:FRAME.size + length], pending[FRAME.size + length:]
                        if kind == b"x":
                            return 0
                        if kind == b"d":
                            os.write(stdout, payload)
    finally:
        events.close()
        signal.set_wakeup_fd(previous)
        os.close(wake_read)
        os.close(wake_write)


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not argv[1].startswith("/"):
        print("usage: python -m acc.broker.container_attach <attach socket> <the unit's cgroup>", file=sys.stderr)
        return 2
    try:
        conn = connect(argv[0])
    except OSError as error:
        print(f"the container's terminal isn't reachable: {error.strerror or error}", file=sys.stderr)
        return 1
    try:
        trusted = relay_is_the_units(conn, argv[1])
    except OSError:
        trusted = False
    if not trusted:
        conn.close()
        print("the socket at the session's attach path isn't the session's terminal (its listener runs outside the "
              "session's unit); nothing was sent", file=sys.stderr)
        return 1
    stdin, stdout = sys.stdin.fileno(), sys.stdout.fileno()
    saved = termios.tcgetattr(stdin) if os.isatty(stdin) else None
    try:
        if saved is not None:
            tty.setraw(stdin, termios.TCSANOW)  # keep keys typed while the pane started
        with conn:
            return run(conn, stdin, stdout)
    finally:
        if saved is not None:
            termios.tcsetattr(stdin, termios.TCSADRAIN, saved)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
