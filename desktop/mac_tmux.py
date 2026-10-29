"""tmux on macOS paths: where the agent's tmux server lives, and recovery after the broker or tmux crashed.

The shared broker selects its server with `tmux -L <label>` (on macOS the short LABEL). Without TMUX_TMPDIR, tmux puts that socket in
/private/tmp/tmux-<uid>/. A reboot clears that folder, and macOS's periodic cleanup of old /tmp files may remove an
idle socket: the server then runs on with nobody able to reach its sessions. On macOS the broker therefore sets
TMUX_TMPDIR to a folder inside the agent's own run directory (the shared code is unchanged; tmux appends
tmux-<uid>/<label>).

`recover` runs when the broker starts, after its configuration has passed the account checks that include the
staff/admin membership gate (broker.configuration -> execution.validate_account -> mac_setup.verify_identity):
- a live server keeps running, and its sessions are listed so the main can reattach them;
- a stale socket (the server died: nothing answers) is removed, but only if it is a socket the agent owns inside its
  private 0700 folder, so tmux can start cleanly;
- a server still running at the old /private/tmp location is reported, not adopted or killed.
"""
from __future__ import annotations

import os
import socket
import stat
import subprocess
import sys
from pathlib import Path

SUN_PATH = 104  # sockaddr_un.sun_path on macOS, including the terminating NUL
# The folder is already this owner's alone, so the server label can be short: the whole path must fit SUN_PATH
# ('/Library/Application Support/Rynhult Desktop/Owners/<uid>/run/rynd-<uid>/tmux/tmux-<agent uid>/rd').
LABEL = 'rd'
FORMAT = '#{session_name}\t#{session_attached}\t#{session_created}'


def tmpdir(value: dict) -> Path:
    """TMUX_TMPDIR for this owner's agent: inside the broker's run directory (run_dir/<agent>)."""
    return Path(value['run_dir']) / value['agent'] / 'tmux'


def socket_path(value: dict, label: str, uid: int | None = None) -> Path:
    uid = value['agent_uid'] if uid is None else uid
    path = tmpdir(value) / f'tmux-{uid}' / label
    if len(os.fsencode(path)) >= SUN_PATH:
        raise ValueError(f'tmux socket path is too long for macOS ({len(os.fsencode(path))} bytes): {path}')
    return path


def legacy_path(label: str, uid: int, platform: str | None = None) -> Path:
    platform = sys.platform if platform is None else platform
    return Path('/private/tmp' if platform == 'darwin' else '/tmp') / f'tmux-{uid}' / label


def prepare(value: dict, uid: int | None = None) -> Path:
    """The private folder exists, is a real directory, is the agent's and is 0700."""
    uid = os.getuid() if uid is None else uid
    folder = tmpdir(value)
    folder.mkdir(mode=0o700, exist_ok=True)
    info = folder.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError(f'{folder} must be a 0700 folder owned by the agent')
    return folder


def probe(path: Path, uid: int | None = None) -> str:
    """'absent', 'alive', 'stale' (a socket nobody answers on) or 'foreign' (not the agent's socket)."""
    uid = os.getuid() if uid is None else uid
    try:
        info = path.lstat()
    except FileNotFoundError:
        return 'absent'
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != uid:
        return 'foreign'
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(2)
    try:
        client.connect(str(path))
        return 'alive'
    except (ConnectionRefusedError, FileNotFoundError):
        return 'stale'
    finally:
        client.close()


def sessions(path: Path, tmux: str = 'tmux') -> list[dict]:
    result = subprocess.run([tmux, '-S', str(path), 'list-sessions', '-F', FORMAT], capture_output=True, text=True,
                            timeout=10)
    if result.returncode != 0:
        return []  # a server with no sessions exits list-sessions with an error
    out = []
    for line in result.stdout.splitlines():
        name, attached, created = (line.split('\t') + ['', '', ''])[:3]
        out.append({'name': name, 'attached': attached not in ('', '0'), 'created': int(created) if created.isdigit() else None})
    return out


def recover(value: dict, label: str, *, uid: int | None = None, tmux: str = 'tmux', platform: str | None = None) -> dict:
    uid = os.getuid() if uid is None else uid
    folder = prepare(value, uid)
    path = socket_path(value, label, uid)
    state, removed = probe(path, uid), False
    if state == 'foreign':
        raise ValueError(f'{path} is not a tmux socket owned by the agent; refusing to use or remove it')
    if state == 'stale':
        parent = path.parent.lstat()
        if stat.S_ISLNK(parent.st_mode) or parent.st_uid != uid or stat.S_IMODE(parent.st_mode) & 0o077:
            raise ValueError(f'{path.parent} is not the agent\'s private tmux folder; refusing to remove the socket')
        path.unlink()
        state, removed = 'absent', True
    legacy = legacy_path(label, uid, platform)
    return {'tmpdir': str(folder), 'socket': str(path), 'state': state, 'removed_stale': removed,
            'sessions': sessions(path, tmux) if state == 'alive' else [],
            'legacy': {'socket': str(legacy), 'state': probe(legacy, uid)} if legacy != path else None}
