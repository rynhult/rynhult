"""The container session pane's client (acc.broker.container_attach) against a relay speaking the guard's frames."""
from __future__ import annotations

import json
import os
import pty
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

FRAME = struct.Struct(">cI")
SRC = str(Path(__file__).resolve().parents[1] / "src")


def frame(kind, payload=b""):
    return FRAME.pack(kind, len(payload)) + payload


@pytest.fixture
def relay_socket():
    folder = Path(tempfile.mkdtemp(prefix="ct-", dir="/tmp"))  # a socket path must fit 108 bytes
    yield folder / "attach.sock"
    shutil.rmtree(folder)


def read_frames(conn, until):
    data, out = b"", []
    conn.settimeout(10)
    while not until(out):
        chunk = conn.recv(65536)
        if not chunk:
            break
        data += chunk
        while len(data) >= FRAME.size and len(data) >= FRAME.size + FRAME.unpack_from(data)[1]:
            kind, length = FRAME.unpack_from(data)
            out.append((kind, data[FRAME.size:FRAME.size + length]))
            data = data[FRAME.size + length:]
    return out


def own_cgroup():
    return Path("/proc/self/cgroup").read_text().strip().split("::", 1)[1]


def client(path, cgroup=None, **kwargs):
    return subprocess.Popen([sys.executable, "-m", "acc.broker.container_attach", str(path), cgroup or own_cgroup()],
                            env={**os.environ, "PYTHONPATH": SRC}, **kwargs)


def test_pane_relays_keys_and_output_and_ends_with_the_container(relay_socket):
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(relay_socket))
    server.listen(1)
    seen, sized = [], threading.Event()

    def relay():
        conn, _ = server.accept()
        with conn:
            seen.extend(read_frames(conn, lambda out: bool(out)))  # the pane's size comes first
            sized.set()
            seen.extend(read_frames(conn, lambda out: any(k == b"d" and b"ls" in p for k, p in out)))
            conn.sendall(frame(b"d", b"agent output\r\n") + frame(b"x"))

    thread = threading.Thread(target=relay)
    thread.start()
    primary, secondary = pty.openpty()
    import fcntl
    import termios
    fcntl.ioctl(secondary, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    proc = client(relay_socket, stdin=secondary, stdout=secondary, stderr=secondary)
    os.close(secondary)
    assert sized.wait(10)
    os.write(primary, b"ls\r")
    output = b""
    deadline = time.monotonic() + 10
    while b"agent output" not in output and time.monotonic() < deadline:
        try:
            output += os.read(primary, 4096)
        except OSError:
            break
    assert proc.wait(10) == 0  # the container ended: so does the pane
    thread.join(5)
    os.close(primary)
    assert b"agent output" in output
    assert seen[0] == (b"r", json.dumps({"rows": 40, "cols": 120}).encode())  # the pane's size first
    assert (b"d", b"ls\r") in seen  # raw keys: no line editing or CR translation on the host side
    server.close()


def test_pane_reports_an_unreachable_terminal(relay_socket):
    """No relay (the unit isn't running): a short message in the pane and a non-zero exit, after the wait."""
    sys.path.insert(0, SRC)
    try:
        from acc.broker import container_attach
    finally:
        sys.path.remove(SRC)
    with pytest.raises(FileNotFoundError):
        container_attach.connect(str(relay_socket), wait=0.3)
    original = container_attach.connect
    container_attach.connect = lambda path: original(path, wait=0)
    try:
        assert container_attach.main([str(relay_socket), own_cgroup()]) == 1
        assert container_attach.main([str(relay_socket)]) == 2
        assert container_attach.main([]) == 2
    finally:
        container_attach.connect = original


def test_the_pane_command_never_imports_from_the_containers_checkout(relay_socket, tmp_path):
    """Review H-1: the pane runs in the session's private checkout, which the agent in the container writes. A planted
    json.py (or an acc package) there must never run as the host account when the pane starts on a resume."""
    sys.path.insert(0, SRC)
    try:
        from acc.broker import containers
    finally:
        sys.path.remove(SRC)
    root = relay_socket.parent
    path = containers.attach_socket(root, "s1a2b3c4d5", 2)
    path.parent.mkdir()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)
    server.settimeout(10)

    def relay():
        try:
            conn, _ = server.accept()
        except OSError:  # a hijacked pane never connects
            return
        with conn:
            conn.sendall(frame(b"x"))

    thread = threading.Thread(target=relay, daemon=True)
    thread.start()
    checkout, marker = tmp_path / "checkout", tmp_path / "escaped"
    (checkout / "acc/broker").mkdir(parents=True)
    planted = f"open({str(marker)!r}, 'w').close()\n"
    for name in ("json.py", "selectors.py", "acc/__init__.py", "acc/broker/__init__.py",
                 "acc/broker/container_attach.py"):
        (checkout / name).write_text(planted)
    # The pane's environment as the broker sets it, plus anything a PYTHON* variable could add.
    env = {"PATH": os.environ["PATH"], "PYTHONPATH": str(checkout)}
    command = containers.attach_command(root, "s1a2b3c4d5", 2)
    assert command[-1] == (f"/acc.slice/acc-containers.slice/acc-containers-{os.getuid()}.slice/"
                           "acc-ct-s1a2b3c4d5-g2.service")  # the unit whose relay the pane accepts
    command[-1] = own_cgroup()  # the fake relay is this test
    proc = subprocess.run(command, cwd=checkout, env=env,
                          input=b"", capture_output=True, timeout=30)
    thread.join(5)
    server.close()
    assert not marker.exists(), proc.stderr
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("listener", ["other-cgroup", "parent-cgroup", "sibling-prefix"])
def test_pane_refuses_a_listener_outside_the_sessions_unit(relay_socket, listener):
    """Review A-4: anything running as the account could put its own socket at the path. The pane checks that the
    listener runs in the session's unit (which the account can't move a process into) before sending a single key."""
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(relay_socket))
    server.listen(1)
    server.settimeout(10)
    got = []

    def impostor():
        conn, _ = server.accept()
        with conn:
            conn.settimeout(5)
            while chunk := conn.recv(65536):
                got.append(chunk)

    thread = threading.Thread(target=impostor, daemon=True)
    thread.start()
    mine = own_cgroup()
    expected = {"other-cgroup": "/acc.slice/acc-containers.slice/acc-containers-1004.slice/acc-ct-s1a2b3c4d5-g1.service",
                "parent-cgroup": mine + "/acc-ct-s1a2b3c4d5-g1.service",
                "sibling-prefix": mine.rstrip("/") + "x"}[listener]
    primary, secondary = pty.openpty()
    proc = client(relay_socket, expected, stdin=secondary, stdout=secondary, stderr=secondary)
    os.close(secondary)
    os.write(primary, b"secret typed early\r")
    try:
        code = proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()
        code = proc.wait(5)
    os.set_blocking(primary, False)
    output = b""
    try:
        while chunk := os.read(primary, 4096):
            output += chunk
    except OSError:
        pass
    os.close(primary)
    thread.join(5)
    server.close()
    assert code == 1 and got == []  # no size, no keys
    assert b"isn't the session's terminal" in output


def test_pane_accepts_its_relay_in_a_child_cgroup_of_the_unit(relay_socket):
    """Podman may move processes into the unit's sub-cgroups; anything below the unit is still the unit."""
    sys.path.insert(0, SRC)
    try:
        from acc.broker import container_attach
    finally:
        sys.path.remove(SRC)
    mine = own_cgroup()
    parent = mine.rsplit("/", 1)[0] or "/"
    assert container_attach.inside(mine, mine) and container_attach.inside(mine, parent)
    assert not container_attach.inside(parent, mine) and not container_attach.inside(mine + "x", mine)
