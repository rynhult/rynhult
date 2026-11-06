"""Unprivileged local backup validation and observations; never invoke root."""
import ctypes
import shlex
from pathlib import Path

from ..backup_server import Refused


def refuse_system_path(path: Path):
    if path == Path('/') or any(path.is_relative_to(root) for root in map(Path, (
        '/etc', '/usr', '/bin', '/sbin', '/lib', '/lib64', '/boot', '/proc', '/sys', '/dev',
        '/var', '/home', '/root', '/srv/acc',
    ))):
        raise Refused('Choose a persistent storage folder outside system and hidden home paths.')


def normalize_folder(folder: str) -> Path:
    if (not folder.startswith('/') or any(p in ('.', '..') for p in folder.split('/'))
            or any(ord(c) < 32 or ord(c) > 126 for c in folder)):
        raise Refused('Choose an absolute ASCII folder path without traversal or control characters.')
    return Path('/' + '/'.join(p for p in folder.split('/') if p))


def repository_path(folder: str, data: Path, same_disk: bool) -> Path:
    path = normalize_folder(folder)
    refuse_system_path(path)
    if '\x00' in folder or not path.is_absolute() or '..' in path.parts or any(p.is_symlink() for p in (path, *path.parents)):
        raise Refused('Choose an absolute folder path without symlinks or parent traversal.')
    if not path.is_dir():
        raise Refused('The folder must already exist.')
    path = path.resolve(strict=True)
    refuse_system_path(path)
    if path.is_relative_to(data.parent.resolve()):
        raise Refused('Choose a folder outside the installation tree.')
    refuse_volatile(path)
    if path.stat().st_dev == data.stat().st_dev and not same_disk:
        raise Refused('This folder is on the same disk. Confirm that you understand this before continuing.')
    import uuid
    return path / ('rynhult-' + uuid.uuid4().hex)


def installed_backup(snapshot_path):
    from .host import read_snapshot
    snapshot = read_snapshot(snapshot_path) or {}
    return snapshot.get('installed_backup') or {
        'repository': '/var/backups/acc', 'last_run': None, 'next_run': None, 'bytes': None,
        'state': 'Status unavailable; host collector update required'}


def refuse_volatile(path: Path):
    if any(path.is_relative_to(Path(root)) for root in ('/tmp', '/var/tmp', '/dev/shm', '/run')):
        raise Refused('Temporary or volatile storage cannot hold backups.')
    # Linux statfs: f_type is the first native long. Keep ample space for the ABI struct.
    buf = ctypes.create_string_buffer(256)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.statfs(bytes(path), ctypes.byref(buf)) != 0:
        raise Refused('Could not verify the storage filesystem.')
    kind = ctypes.c_long.from_buffer(buf).value
    if kind in (0x01021994, 0x858458F6):  # TMPFS_MAGIC, RAMFS_MAGIC
        raise Refused('Temporary or volatile storage cannot hold backups.')


def folder_command(folder: str, same_disk: bool) -> str:
    path = normalize_folder(folder)
    if '\x00' in folder or any(ord(c) < 32 for c in folder) or not path.is_absolute() or '..' in path.parts:
        raise Refused('Choose an absolute folder path without parent traversal or control characters.')
    refuse_system_path(path)
    # accd cannot inspect paths hidden by its sandbox. Root does the authoritative checks.
    if any(path.is_relative_to(Path(root)) for root in ('/tmp', '/var/tmp', '/dev/shm', '/run')):
        raise Refused('Temporary or volatile storage cannot hold backups.')
    return 'sudo rynhult-admin backup add-folder ' + ('--same-disk ' if same_disk else '') + '-- ' + shlex.quote(str(path))
