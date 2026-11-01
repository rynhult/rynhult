"""Bounded project file operations, executed only by an agent's broker."""
from __future__ import annotations

import base64
import ctypes
import fcntl
import hashlib
import os
import re
import secrets
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from .ops import OpError, _read_at, _walk_dirs

MAX_TEXT = 1024 * 1024
MAX_UPLOAD = 4 << 20
MAX_UPLOAD_BATCH = 16 << 20
MAX_DOWNLOAD = 4 * 1024 * 1024
MAX_ENTRIES = 2000
# Concrete-path equivalent of bin/acc-guard's secrets rule. Keep parity tests in test_files.py.
SECRET_NAMES = re.compile(r'^(\.env(\..+)?|session\.env|\.netrc|\.git-credentials|\.pgpass|id_(rsa|dsa|ecdsa|ed25519)|.+\.(pem|p12|pfx)|credentials\.json|service-account.*\.json)$')
SAFE_ENV = re.compile(r'^\.env\.(example|sample|template|dist|defaults)$')
CREDENTIAL_DIRS = ('.ssh', '.gnupg', '.aws', '.docker', '.kube', '.config/gh')
CREDENTIAL_FILES = ('.codex/auth.json', '.claude/.credentials.json', '.copilot/config.json',
    '.config/antigravity/plugin-secrets.json', '.gemini/antigravity-cli/antigravity-oauth-token', '.config/cursor/auth.json',
    '.local/share/opencode/opencode.db', '.local/share/opencode/opencode.db-wal',
    '.local/share/opencode/opencode.db-shm', '.local/share/opencode/auth.json')


def parts(rel: str, directory=False) -> list[str]:
    if not isinstance(rel, str) or rel.startswith('/') or '\\' in rel or '\0' in rel or len(rel) > 4096:
        raise OpError('Use a path inside this folder.')
    if rel == '' and directory:
        return []
    result = rel.split('/')
    if any(p in ('', '.', '..') for p in result):
        raise OpError('Use a path without . or .. in it.')
    return result


def protected(config, root: Path, rel: str) -> bool:
    full = root / rel
    if full == config.run_dir or config.run_dir in full.parents or '/srv/acc/data/secrets/' in str(full) + '/':
        return True
    names = full.parts
    if '.git' in names:
        return True
    for i, name in enumerate(names):
        if SECRET_NAMES.fullmatch(name) and not SAFE_ENV.fullmatch(name) and not name.endswith('.pub'):
            return True
        suffix = '/'.join(names[i:])
        if any(suffix == p or suffix.startswith(p + '/') for p in CREDENTIAL_DIRS):
            return True
        if suffix in CREDENTIAL_FILES:
            return True
    # Also protect credential folders if the registered root is itself inside one.
    absolute = str(full)
    return any('/' + p + '/' in absolute + '/' for p in CREDENTIAL_DIRS) or any(absolute.endswith('/' + p) for p in CREDENTIAL_FILES)


def regular(info):
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
        raise OpError('Links and special files can\'t be opened here. Open the file it points to instead.')


def _open_dir(root, components):
    """Walk from a pinned descriptor, refusing symlinks in every component."""
    fd = os.dup(root) if isinstance(root, int) else os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for index, component in enumerate(components):
            access = getattr(os, 'O_PATH', os.O_RDONLY) if index < len(components) - 1 else os.O_RDONLY
            nxt = os.open(component, access | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def workspace(config, path):
    # Do not resolve an agent-controlled path then reopen it. Even the configured allowed
    # root is opened component by component, starting at the filesystem root.
    if '..' in Path(path).parts:
        raise OpError('Use a workspace path without .. in it.')
    root = Path(os.path.abspath(path))
    if config.folder_revision:
        from acc.folder_policy import FolderError, broker_path
        try:
            broker_path(config, path)
        except FolderError as error:
            raise OpError(str(error)) from None
    anchors = [Path(os.path.abspath(p)) for p in config.allowed_roots] if config.allowed_folders else [root]
    anchor = next((p for p in anchors if root == p or p in root.parents), None)
    if anchor is None:
        raise OpError('This folder is outside the agent account\'s workspace folders, so Rynhult won\'t open it.')
    if protected(config, root, ''):
        raise OpError('This file or folder is protected (keys, passwords or Git\'s own files), so it can\'t be opened here.')
    allowed_fd = _walk_dirs(list(root.parts[1:]))
    try:
        # Listing and flock need a readable descriptor, but ancestors only need traversal.
        root_fd = os.open('.', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=allowed_fd)
    finally:
        os.close(allowed_fd)
    try:
        yield root, root_fd
    finally:
        os.close(root_fd)


@contextmanager
def parent(config, root, root_fd, rel):
    components = parts(rel)
    if protected(config, root, rel):
        raise OpError('This file or folder is protected (keys, passwords or Git\'s own files), so it can\'t be opened here.')
    fd = _open_dir(root_fd, components[:-1])
    try:
        yield fd, components[-1]
    finally:
        os.close(fd)


def revision(data):
    return hashlib.sha256(data).hexdigest()


def describe(data):
    result = {'size': len(data), 'revision': revision(data)}
    if len(data) > MAX_TEXT:
        return {**result, 'too_large': True}
    media = ('image/png' if data.startswith(b'\x89PNG\r\n\x1a\n') else 'image/jpeg' if data.startswith(b'\xff\xd8\xff')
             else 'image/gif' if data.startswith((b'GIF87a', b'GIF89a'))
             else 'image/webp' if data[:4] == b'RIFF' and data[8:12] == b'WEBP'
             else 'audio/wav' if data[:4] == b'RIFF' and data[8:12] == b'WAVE'
             else 'audio/mpeg' if data.startswith(b'ID3') or (len(data) >= 2 and data[0] == 0xff and data[1] & 0xe0 == 0xe0)
             else 'audio/ogg' if data.startswith(b'OggS')
             else 'audio/mp4' if data[4:8] == b'ftyp' and data[8:12] in (b'M4A ', b'M4B ')
             else 'video/mp4' if data[4:8] == b'ftyp' and data[8:12] in (b'isom', b'iso2', b'mp41', b'mp42', b'avc1', b'M4V ', b'dash')
             else 'video/webm' if data.startswith(b'\x1a\x45\xdf\xa3') and b'\x42\x82\x84webm' in data[:4096] else None)
    try:
        if b'\0' in data or media:
            raise UnicodeError()
        content = data.decode('utf-8')
        if any(ord(c) < 32 and c not in '\r\n\t\f' for c in content):
            raise UnicodeError()
        return {**result, 'content': content, 'binary': False}
    except UnicodeError:
        return {**result, 'binary': True, 'media_type': media}


def git(root, *args, env=None, input=None):
    clean = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    clean.update({'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_TERMINAL_PROMPT': '0'})
    clean.update(env or {})
    # Child git processes inherit only the pinned workspace descriptor. No mutable
    # workspace pathname is reopened for status or save-and-commit.
    inherited = (root,) if isinstance(root, int) else ()
    location = f"{'/dev/fd' if sys.platform == 'darwin' else '/proc/self/fd'}/{root}" if inherited else str(root)
    out = subprocess.run(['git', '--literal-pathspecs', '-C', location, '-c', 'core.hooksPath=/dev/null',
                          '-c', 'core.fsmonitor=false', '-c', 'commit.gpgsign=false', *args], input=input,
                         capture_output=True, env=clean, timeout=30, pass_fds=inherited)
    if out.returncode:
        raise OpError('Git couldn\'t do that. Check that the folder is a Git repository on a branch.')
    return out.stdout.decode('utf-8', errors='replace').rstrip('\n')


def commit(root, rel, data, mode, message, name, email):
    if not message.strip() or len(message) > 2000 or not name or not email or any(c in name + email for c in '\r\n\0<>') or not re.fullmatch(r'[^\s@]+@[^\s@]+', email):
        raise OpError('Set your commit name and email, and enter a commit message.')
    branch = git(root, 'symbolic-ref', '-q', 'HEAD')  # detached heads are refused
    try:
        old = git(root, 'rev-parse', '--verify', 'HEAD')
    except OpError:
        old = None  # new local workspace
    # A private index commits precisely the editor bytes, bypassing filters and unrelated staged work.
    import tempfile
    with tempfile.TemporaryDirectory(prefix='acc-files-') as temp:
        env = {'GIT_INDEX_FILE': str(Path(temp) / 'index'), 'GIT_AUTHOR_NAME': name, 'GIT_AUTHOR_EMAIL': email,
               'GIT_COMMITTER_NAME': name, 'GIT_COMMITTER_EMAIL': email}
        git(root, 'read-tree', old or '--empty', env=env)
        blob = git(root, 'hash-object', '-w', '--stdin', input=data)
        filemode = '100755' if mode & 0o111 else '100644'
        git(root, 'update-index', '--add', '--cacheinfo', f'{filemode},{blob},{rel}', env=env)
        tree = git(root, 'write-tree', env=env)
        sha = git(root, 'commit-tree', tree, *(['-p', old] if old else []), env=env, input=(message + '\n').encode())
        git(root, 'update-ref', branch, sha, old or '0' * 40)
        warning = None
        try:
            git(root, 'update-index', '--add', '--cacheinfo', f'{filemode},{blob},{rel}')
        except OpError:
            # The branch update already succeeded: report/audit the commit even if another git command holds
            # the working index. Never undo the commit or remove somebody else's index lock.
            warning = 'Commit created, but the working index could not refresh. Check staged changes through your agent.'
    return {'commit': sha, **({'commit_warning': warning} if warning else {})}


def operate(config, path, action='list', rel='', content='', revision=None, destination='', message=None, name=None, email=None, data='', overwrite='refuse'):
    with workspace(config, path) as (root, root_fd):
        return _operate(config, root, root_fd, action, rel, content, revision, destination, message, name, email, data, overwrite)


def _operate(config, root, root_fd, action, rel, content, revision, destination, message, name, email, data, overwrite):
    components = parts(rel, directory=action == 'list')
    if protected(config, root, rel):
        raise OpError('This file or folder is protected (keys, passwords or Git\'s own files), so it can\'t be opened here.')
    if action == 'list':
        fd = _open_dir(root_fd, components)
        try:
            with os.scandir(fd) as listing:
                entries = []
                for entry in listing:
                    info = entry.stat(follow_symlinks=False)
                    kind = 'directory' if stat.S_ISDIR(info.st_mode) else 'file' if stat.S_ISREG(info.st_mode) else 'unsupported'
                    child = '/'.join([*components, entry.name])
                    entries.append({'name': entry.name, 'path': child, 'size': info.st_size, 'kind': kind,
                                    'protected': protected(config, root, child), 'status': ''})
                    if len(entries) > MAX_ENTRIES:
                        raise OpError('This folder has more than 2000 entries, too many to list. Open a subfolder instead.')
            entries.sort(key=lambda e: (e['kind'] != 'directory', e['name'].casefold()))
            # Limit git's scan to the directory requested (one level).
            if entries:
                try:
                    status = git(root_fd, 'status', '--porcelain=v1', '--ignored', '--untracked-files=normal', '-z', '--', rel or '.')
                    markers = {}
                    for row in status.split('\0'):
                        if len(row) >= 4:
                            markers[row[3:].rstrip('/')] = 'ignored' if row[:2] == '!!' else 'new' if row[:2] in ('??', 'A ') else 'modified'
                    for entry in entries:
                        entry['status'] = markers.get(entry['path'], '')
                except OpError:
                    pass  # plain folders have no git status
            return {'entries': entries}
        finally:
            os.close(fd)
    with parent(config, root, root_fd, rel) as (fd, leaf):
        # Lock the directory descriptor: no lockfile can be redirected by an agent.
        fcntl.flock(fd, fcntl.LOCK_EX)
        if action == 'upload':
            if overwrite not in ('refuse', 'replace', 'keep-both'):
                raise OpError('Unknown upload conflict choice.')
            if not isinstance(data, str) or len(data) > ((MAX_UPLOAD + 2) // 3) * 4:
                raise OpError('Upload exceeds the 4 MiB per-file limit.')
            try:
                raw = base64.b64decode(data, validate=True)
            except ValueError:
                raise OpError('Invalid upload encoding.') from None
            if len(raw) > MAX_UPLOAD:
                raise OpError('Upload exceeds the 4 MiB per-file limit.')
            original = None
            mode = 0o644
            if _exists(fd, leaf):
                original = _read_at(fd, leaf, MAX_UPLOAD)
                if len(original) > MAX_UPLOAD:
                    raise OpError('Existing file exceeds the 4 MiB replacement limit.')
                conflict = {'conflict': True, 'revision': hashlib.sha256(original).hexdigest()}
                if overwrite == 'refuse' or (overwrite == 'replace' and revision != conflict['revision']):
                    return conflict
                if overwrite == 'keep-both':
                    stem, suffix = os.path.splitext(leaf)
                    for number in range(1, 1001):
                        candidate = f'{stem} ({number}){suffix}'
                        candidate_rel = '/'.join([*components[:-1], candidate])
                        if not _exists(fd, candidate) and not protected(config, root, candidate_rel):
                            leaf, rel, original = candidate, candidate_rel, None
                            break
                    else:
                        raise OpError('No free keep-both filename. Rename the upload and try again.')
                else:
                    mode = stat.S_IMODE(os.stat(leaf, dir_fd=fd, follow_symlinks=False).st_mode) & 0o777
            elif overwrite == 'replace':
                raise OpError('The original file disappeared. Refresh the folder before uploading again.')
            temp = '.acc-save-' + secrets.token_hex(12)
            try:
                out = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=fd)
                with os.fdopen(out, 'wb') as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                if original is None:
                    os.link(temp, leaf, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
                else:
                    kept = replace_preserving(fd, leaf, temp, rel, original, MAX_UPLOAD)
                    if kept.get('conflict'):
                        return {k: v for k, v in kept.items() if k != 'current'}
                os.fsync(fd)
            finally:
                if _exists(fd, temp):
                    os.unlink(temp, dir_fd=fd)
            return {'path': rel, 'size': len(raw), 'revision': hashlib.sha256(raw).hexdigest(),
                    **(kept if original is not None else {})}
        if action in ('read', 'download'):
            data = _read_at(fd, leaf, MAX_DOWNLOAD if action == 'download' else MAX_TEXT)
            if action == 'download':
                if len(data) > MAX_DOWNLOAD:
                    raise OpError('This file is too large to download here (4 MiB limit).')
                return {'data': base64.b64encode(data).decode(), **describe(data)}
            return describe(data)
        if action in ('write', 'create'):
            data = content.encode('utf-8')
            if len(data) > MAX_TEXT or b'\0' in data:
                raise OpError('The text is too long (1 MiB limit) or contains a null character.')
            mode = 0o644
            if message is not None:
                # Validate before changing the file.
                if not name or not email or not message.strip() or any(c in name + email for c in '\r\n\0<>') or not re.fullmatch(r'[^\s@]+@[^\s@]+', email):
                    raise OpError('Set your commit name and email, and enter a commit message.')
            if action == 'write':
                original = _read_at(fd, leaf, MAX_TEXT)
                if len(original) > MAX_TEXT or describe(original).get('binary'):
                    raise OpError('Only text files up to 1 MiB can be edited here.')
                if revision != hashlib.sha256(original).hexdigest():
                    return {'conflict': True, 'current': describe(original)}
                mode = stat.S_IMODE(os.stat(leaf, dir_fd=fd, follow_symlinks=False).st_mode) & 0o777
            temp = '.acc-save-' + secrets.token_hex(12)
            try:
                out = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=fd)
                with os.fdopen(out, 'wb') as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                if action == 'create':
                    os.link(temp, leaf, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
                else:
                    kept = replace_preserving(fd, leaf, temp, rel, original, MAX_TEXT)
                    if kept.get('conflict'):
                        return kept
                os.fsync(fd)
            finally:
                os.unlink(temp, dir_fd=fd) if _exists(fd, temp) else None
            result = {**describe(data), **(kept if action == 'write' else {})}
            if message is not None:
                try:
                    result.update(commit(root_fd, rel, data, mode, message, name, email))
                except (OpError, subprocess.TimeoutExpired):
                    result['commit_error'] = 'Saved, but commit failed. Check the current branch and retry with Save and commit.'
            return result
        if action == 'mkdir':
            os.mkdir(leaf, dir_fd=fd)
            return {'ok': True}
        info = os.stat(leaf, dir_fd=fd, follow_symlinks=False)
        regular(info)
        if action in ('delete', 'rename') and stat.S_ISDIR(info.st_mode):
            sub = _open_dir(root_fd, components)
            try:
                if action == 'delete' and os.listdir(sub):
                    raise OpError('The folder must be empty before you delete it.')
                if action == 'rename':
                    validate_tree(config, root, rel, sub, destination=destination)
            finally:
                os.close(sub)
        if action == 'delete':
            (os.rmdir if stat.S_ISDIR(info.st_mode) else os.unlink)(leaf, dir_fd=fd)
            return {'ok': True}
        if action == 'rename':
            with parent(config, root, root_fd, destination) as (dest_fd, dest_leaf):
                rename_exclusive(fd, leaf, dest_fd, dest_leaf)
            return {'ok': True}
        raise OpError('unknown file operation')


def _exists(fd, name):
    try:
        os.stat(name, dir_fd=fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def keep_copy(fd, name, kind):
    """Keep a directory entry without following it or overwriting an existing name.

    Copies remain until explicitly deleted. Automatic pruning could destroy the
    only surviving external edit, so these are ordinary hidden workspace files.
    """
    for _ in range(10):
        kept = f'.rynhult-{kind}-{time.time_ns()}-{secrets.token_hex(12)}'
        try:
            rename_exclusive(fd, name, fd, kept)
            return kept
        except FileExistsError:
            continue
    raise OpError('Could not choose an unused recovery filename. Reload the folder and try again.')


def replace_preserving(fd, leaf, temp, rel, original, limit):
    """Move aside, compare the moved entry, then publish without overwriting.

    External writers need not take our advisory lock. Their moved inode remains
    reachable even if they write through an already-open descriptor after the
    comparison. A recreated destination is never replaced.
    """
    prefix = rel.rpartition('/')[0]

    def path(name):
        return f'{prefix}/{name}' if prefix else name

    # Keep the submitted bytes first, even if moving/restoring the original or
    # publishing later fails. The caller may safely clean its staging filename.
    incoming = keep_copy(fd, temp, 'incoming')
    os.fsync(fd)
    try:
        previous = keep_copy(fd, leaf, 'replaced')
    except (OSError, OpError):
        return replacement_conflict(fd, leaf, rel, limit, path(incoming), rel if _exists(fd, leaf) else None)
    os.fsync(fd)
    try:
        unchanged = _read_at(fd, previous, limit) == original
    except OSError:
        unchanged = False  # a raced-in link/special file is never followed
    if unchanged:
        try:
            os.link(incoming, leaf, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
        except OSError:
            pass
        else:
            os.fsync(fd)
            os.unlink(incoming, dir_fd=fd)
            os.fsync(fd)
            return {'previous_copy': path(previous),
                    'notice': f'Replaced {rel} (the previous copy is kept as {path(previous)}).'}
    # Restore the displaced entry only if nobody recreated the destination.
    # Rename moves a symlink itself; it never opens its target.
    try:
        rename_exclusive(fd, previous, fd, leaf)
        previous = leaf
    except (OSError, OpError):
        pass  # both kept entries remain reachable if restoration is unavailable
    os.fsync(fd)
    return replacement_conflict(fd, leaf, rel, limit, path(incoming), path(previous))


def replacement_conflict(fd, leaf, rel, limit, incoming, previous):
    try:
        current = describe(_read_at(fd, leaf, limit))
    except OSError:
        current = {'binary': True}
    return {'conflict': True, 'current': current, 'revision': current.get('revision'),
            **({'previous_copy': previous} if previous else {}), 'incoming_copy': incoming,
            'notice': (f'{rel} changed or could not be replaced. '
                       + (f'Both copies are kept: {previous} and {incoming}. ' if previous else
                          f'The original disappeared. Your incoming copy is kept as {incoming}. ')
                       + 'Reload the folder before trying again.')}


def validate_tree(config, root, rel, fd, budget=None, destination=None):
    """No moving credential folders or unsupported nodes via an innocent-looking parent."""
    budget = [MAX_ENTRIES] if budget is None else budget
    with os.scandir(fd) as listing:
        for entry in listing:
            budget[0] -= 1
            if budget[0] < 0:
                raise OpError('This folder has too many files (over 2000) for Rynhult to check before renaming it. Ask the agent to rename it in a session instead.')
            child = rel + '/' + entry.name
            dest_child = destination + '/' + entry.name if destination is not None else child
            if protected(config, root, child) or protected(config, root, dest_child):
                raise OpError('This folder contains protected files (keys, passwords or Git\'s own files), so it can\'t be renamed here.')
            info = entry.stat(follow_symlinks=False)
            regular(info)
            if stat.S_ISDIR(info.st_mode):
                sub = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    validate_tree(config, root, child, sub, budget, destination=dest_child)
                finally:
                    os.close(sub)


def rename_exclusive(src_fd, src, dst_fd, dst):
    """An atomic rename that cannot overwrite a concurrently created destination."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == 'darwin':
        function, flag = getattr(libc, 'renameatx_np', None), 4  # RENAME_EXCL
    else:
        function, flag = getattr(libc, 'renameat2', None), 1  # RENAME_NOREPLACE
    if function is None:
        raise OpError('This machine can\'t rename files safely, so renaming isn\'t available here.')
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    if function(src_fd, os.fsencode(src), dst_fd, os.fsencode(dst), flag):
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number))
