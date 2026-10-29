"""launchd socket activation for the unprivileged macOS desktop broker."""
from __future__ import annotations

import asyncio
import ctypes
import os
import socket
import stat
import sys
from pathlib import Path

RESOURCES = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(RESOURCES / 'desktop'), str(RESOURCES / 'src')]

from broker import configuration  # noqa: E402
from mac_setup import protected  # noqa: E402

from acc.broker import ops  # noqa: E402
from acc.broker.server import MAX_LINE, Broker  # noqa: E402


def launch_fds() -> list[int]:
    if sys.platform != 'darwin':
        raise ValueError('launchd activation is available only on macOS')
    library = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
    activate = library.launch_activate_socket
    activate.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.POINTER(ctypes.c_int)), ctypes.POINTER(ctypes.c_size_t)]
    activate.restype = ctypes.c_int
    library.free.argtypes = [ctypes.c_void_p]
    library.free.restype = None
    fds = ctypes.POINTER(ctypes.c_int)()
    count = ctypes.c_size_t()
    result = activate(b'Broker', ctypes.byref(fds), ctypes.byref(count))
    if result:
        raise ValueError(f'Protected launchd Broker socket unavailable: {os.strerror(result)}')
    try:
        return [fds[i] for i in range(count.value)]
    finally:
        library.free(fds)


def check_socket_path(path: Path, owner_uid: int) -> None:
    protected(path.parents)
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != owner_uid or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError('Expected owner-only launchd socket with mode 0600')


def activated_listener(path: Path, owner_uid: int) -> socket.socket:
    fds = launch_fds()
    if len(fds) != 1 or any(fd < 3 for fd in fds):
        for fd in fds:
            if fd >= 3:
                os.close(fd)
        raise ValueError('Expected exactly one launchd Broker listener')
    listener = socket.socket(fileno=fds[0])
    try:
        if (listener.family != socket.AF_UNIX or listener.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM
                or not listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)):
            raise ValueError('launchd descriptor is not a UNIX stream listener')
        if listener.getsockname() != str(path):
            raise ValueError('launchd listener path differs from protected configuration')
        check_socket_path(path, owner_uid)
        return listener
    except BaseException:
        listener.close()
        raise


async def serve(config, listener, value=None):
    if value is not None:
        # After a crash: keep a live server's sessions, clear a dead server's socket (mac_tmux.py).
        import json

        from mac_tmux import recover
        print('tmux recovery:', json.dumps(recover(value, config.tmux_socket)), file=sys.stderr, flush=True)
    # The account's login shell stays /usr/bin/false. Only job commands use /bin/sh.
    # prepare_tmux first disables exit-empty: an earlier shell override could
    # disappear with the temporary empty server before its persistent setup.
    ops.prepare_tmux(config)
    ops.tmux(config, 'set-option', '-g', 'default-shell', '/bin/sh', check=True)
    handler = Broker(config)
    server = await asyncio.start_unix_server(handler.handle, sock=listener, limit=MAX_LINE)
    async with server:
        await server.serve_forever()


def main():
    from execution import read_manifest
    uid = int(sys.argv[1])
    value = read_manifest(uid)
    if value is None:
        raise ValueError('macOS desktop execution is not provisioned')
    # Activate before clearing inheritance, and never translate it into systemd env/fd 3.
    # Replacing fd 3 after asyncio starts would corrupt its event-loop descriptor.
    listener = activated_listener(Path(value['socket']), uid)
    try:
        config = configuration(uid)  # includes the staff/admin membership gate (mac_setup.verify_identity)
        asyncio.run(serve(config, listener, value))
    finally:
        listener.close()


if __name__ == '__main__':
    main()
