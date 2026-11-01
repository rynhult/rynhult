"""Target-local folder validation shared by the fixed worker and account broker.

No command execution, account lookup selected by a request, or OS grants here.
"""
from __future__ import annotations

import os
import stat
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import PurePosixPath


class FolderError(ValueError):
    pass


SYSTEM = ('/etc', '/root', '/proc', '/sys', '/dev', '/run', '/boot', '/usr', '/bin', '/sbin',
          '/lib', '/lib32', '/lib64', '/tmp', '/var/tmp')
CREDENTIALS = {'.ssh', '.gnupg', '.aws', '.azure', '.kube', '.docker', '.config', '.local',
               '.claude', '.codex', '.copilot', '.gemini', '.mozilla', 'Library'}


def literal_path(path):
    try:
        encoded = path.encode('utf-8') if isinstance(path, str) else b''
    except UnicodeError:
        raise FolderError('Enter a valid UTF-8 folder path without control characters.') from None
    if (not isinstance(path, str) or not path.startswith('/') or len(encoded) > 4096
            or any(unicodedata.category(c) in ('Cc', 'Cf', 'Cs') for c in path) or any(c in path for c in ':$~\\')
            or any(p in ('', '.', '..') for p in path.split('/')[1:]) or len(path.split('/')) > 128):
        raise FolderError('Enter one absolute folder without links, expansion or ambiguous components.')
    return PurePosixPath(path)


def overlaps(a, b):
    return a == b or a in b.parents or b in a.parents


def validate_path(path, *, homes, protected):
    candidate = literal_path(path)
    if any(overlaps(candidate, PurePosixPath(p)) for p in (*SYSTEM, *protected)):
        raise FolderError('System, temporary and service folders and their parents cannot be allowed.')
    home = next((PurePosixPath(p) for p in homes if PurePosixPath(p) in candidate.parents), None)
    if home is None or any(part in CREDENTIALS for part in candidate.relative_to(home).parts):
        raise FolderError('Choose a repository subtree of the associated owner or agent home.')
    return str(candidate)


def mount_id(fd):
    try:
        with open(f'/proc/self/fdinfo/{fd}') as file:
            for line in file:
                if line.startswith('mnt_id:'):
                    return int(line.split(':')[1])
    except OSError:
        pass
    raise FolderError('Safe mount identity checks are unavailable on this machine.')



def mount_type(fd):
    wanted = mount_id(fd)
    with open('/proc/self/mountinfo') as file:
        for number, line in enumerate(file):
            if number >= 10000 or len(line) > 16384:
                break
            prefix, separator, suffix = line.partition(' - ')
            if separator and prefix.split(' ', 1)[0] == str(wanted):
                return suffix.split(' ', 1)[0].lower()
    raise FolderError('Safe filesystem-type checks are unavailable. Use existing access or local setup.')


def inode(fd):
    st = os.fstat(fd)
    return [st.st_dev, st.st_ino, mount_id(fd)]


@dataclass
class Directory:
    fd: int
    identity: list[int]


@contextmanager
def open_directory(path, *, expected=None, root_fd=None, search_only=False, same_mount=False):
    parts = literal_path(path).parts[1:]
    search = getattr(os, 'O_PATH', os.O_RDONLY)
    fd = os.dup(root_fd) if root_fd is not None else os.open('/', search | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for index, part in enumerate(parts):
            flags = search if search_only or index < len(parts) - 1 else os.O_RDONLY
            child = os.open(part, flags | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            if same_mount:
                try:
                    if mount_id(child) != mount_id(fd):
                        raise FolderError('Mount roots, nested crossings and bind aliases need administrator provisioning.')
                except BaseException:
                    os.close(child)
                    raise
            os.close(fd)
            fd = child
        identity = inode(fd)
        if expected is not None and identity != expected:
            raise FolderError('The folder changed since preview. Check it again.')
        yield Directory(fd, identity)
    except OSError:
        raise FolderError('The folder is unavailable or contains a link. Check it again.') from None
    finally:
        os.close(fd)


def trusted_file(fd, *, owner=0):
    st = os.fstat(fd)
    if st.st_uid != owner or st.st_nlink != 1 or not stat.S_ISREG(st.st_mode) or st.st_mode & 0o022:
        raise FolderError('The installed folder policy is not trusted. Ask the administrator to repair it.')


def broker_path(config, path):
    """Mandatory boundaries remain even when the configurable root filter is Off."""
    candidate = literal_path(path)
    protected = (*SYSTEM, *config.folder_protected, str(config.run_dir), '/srv/acc/data',
                 '/srv/acc/app', '/srv/acc/backup-service', '/srv/acc/config')
    if any(overlaps(candidate, PurePosixPath(p)) for p in protected):
        raise FolderError('System and service folders remain protected. Choose a repository folder.')
    if candidate.parts[1:2] == ('home',):
        homes = tuple(PurePosixPath(p) for p in config.folder_homes)
        if not any(p in candidate.parents for p in homes):
            raise FolderError('Other home folders require administrator provisioning.')
    return candidate


def reject_nested_mounts(fd):
    """Do not let a repository operation traverse an already mounted alias below it."""
    location = os.readlink(f'/proc/self/fd/{fd}')
    candidate = PurePosixPath(location)
    try:
        with open('/proc/self/mountinfo') as file:
            for number, line in enumerate(file):
                if number >= 10000 or len(line) > 16384:
                    raise FolderError('Safe mount inventory exceeds its validation budget.')
                fields = line.split(' - ', 1)[0].split()
                if len(fields) < 6:
                    raise FolderError('Safe mount inventory is unavailable.')
                # Linux mountinfo escapes space, tab, newline and backslash.
                point = fields[4]
                for escaped, value in (('\\040', ' '), ('\\011', '\t'), ('\\012', '\n'), ('\\134', '\\')):
                    point = point.replace(escaped, value)
                if candidate in PurePosixPath(point).parents:
                    raise FolderError('Nested mounts and bind aliases need administrator provisioning.')
    except OSError:
        raise FolderError('Safe mount inventory is unavailable.') from None
