"""Same-host private group checkouts. No cross-person filesystem copies.

Session worktrees are standalone clones rather than linked Git administration:
member-controlled config in one session cannot execute during another's sync.
"""
from __future__ import annotations

import base64
import os
import re
import tempfile
from pathlib import Path

from .. import git_exchange as git
from .config import BrokerConfig

PLACEMENT = re.compile(r'^groups/[0-9a-f]{32}/[0-9]+/[0-9a-f]{32}$')
SESSION = re.compile(r'^[0-9a-f]{12,32}$')
MAX_CHECKOUT_BYTES = 128 << 20
MAX_CHECKOUT_ENTRIES = 5000


def private_path(config: BrokerConfig, path: str, create=False) -> Path:
    selected = Path(path)
    root = next((r for r in config.allowed_roots if selected.is_relative_to(r)), None)
    if root is None or not PLACEMENT.fullmatch(str(selected.relative_to(root))):
        raise ValueError('This group work folder isn\'t where Rynhult set it up. Ask an instance owner to set up your group placement again.')
    # Start from the deployment-approved root. Every component below it must be
    # private, owned by this broker and free of inherited ACLs and symlinks.
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        current = root
        for name in selected.relative_to(root).parts:
            if create:
                try:
                    os.mkdir(name, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            current = current / name
            git.private_directory(current)
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
    finally:
        os.close(fd)
    return selected


def provision(config: BrokerConfig, path: str) -> dict:
    selected = private_path(config, path, create=True)
    git.initialize(selected / '.git', str(selected))
    return {'path': str(selected), 'trust': 'approved', 'sync_status': 'ready'}


def send(config: BrokerConfig, path: str, session_id: str, commit: str) -> dict:
    selected = private_path(config, path)
    if not SESSION.fullmatch(session_id):
        raise ValueError('Rynhult sent an invalid group session ID.')
    source = selected / 'sessions' / session_id
    git.private_directory(source)
    git.private_directory(source / '.git')
    return {'commit': commit, 'data': base64.b64encode(git.export(source / '.git', commit)).decode()}


def checkout_budget(repo: Path, commit: str) -> None:
    """Count expanded paths, including duplicate blobs, before writing a tree."""
    entries = git.run(repo, 'ls-tree', '-r', '-t', '-l', '-z', '--full-tree', commit).split(b'\0')
    if len(entries) - 1 > MAX_CHECKOUT_ENTRIES:
        raise ValueError('This shared work has more than 5000 files and folders, too many to check out. Share a smaller change.')
    total = 0
    for entry in entries:
        if not entry:
            continue
        metadata, path = entry.split(b'\t', 1)
        if len(path) > 1024 or path.count(b'/') >= 64:
            raise ValueError('This shared work has a file path that is too long or too deeply nested to check out.')
        size = metadata.split()[-1]
        if size.isdigit():
            total += int(size)
        if total > MAX_CHECKOUT_BYTES:
            raise ValueError('This shared work holds more than 128 MiB of files, too much to check out. Share a smaller change.')


def receive(config: BrokerConfig, path: str, data: str, commit: str, session_id: str) -> dict:
    selected = private_path(config, path)
    if not SESSION.fullmatch(session_id) or len(data) > 4 * -(-git.LIMIT // 3):
        raise ValueError('The shared work Rynhult received isn\'t valid.')
    contents = base64.b64decode(data, validate=True)
    # Validate and strip unrelated objects before creating any visible checkout.
    with tempfile.TemporaryDirectory(prefix='acc-group-import-') as scratch:
        staged = git.initialize(Path(scratch) / 'repo', 'quarantine')
        git.import_pack(staged, contents)
        clean = git.export(staged, commit)
        checkout_budget(staged, commit)
    sessions = selected / 'sessions'
    sessions.mkdir(mode=0o700, exist_ok=True)
    git.private_directory(sessions)
    target = sessions / session_id
    target.mkdir(mode=0o700)  # never overwrite dirty work or an existing session
    try:
        repo = git.initialize(target / '.git', str(target))
        git.run(repo, 'config', 'core.bare', 'false')
        git.publish(repo, clean, 'refs/heads/acc/' + session_id, git.ZERO, commit)
        # Fresh local config, empty template, no submodule initialization or
        # contributor attributes/config filters. Symlink blobs stay symlinks.
        git.run(repo, '--work-tree', str(target), 'checkout', '-q', 'acc/' + session_id)
    except Exception:
        import shutil
        shutil.rmtree(target)
        raise
    return {'path': str(target), 'commit': commit, 'source_review_required': True}


def refresh_context(config: BrokerConfig, session_id: str, content: str) -> dict:
    """Update only the board attachment, pinned beneath this broker's run root. Never touch env."""
    if not SESSION.fullmatch(session_id) or not isinstance(content, str) or len(content.encode()) > 65536:
        raise ValueError('The group board note for this session is too large or isn\'t valid.')
    from .ops import _walk_dirs
    root = _walk_dirs(list(config.run_dir.absolute().parts[1:]))
    try:
        folder = os.open(session_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
    finally:
        os.close(root)
    name = '.group-context-' + os.urandom(8).hex()
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=folder)
        with os.fdopen(fd, 'w') as handle:
            handle.write(content)
        os.rename(name, 'group-context.json', src_dir_fd=folder, dst_dir_fd=folder)
    finally:
        try:
            os.unlink(name, dir_fd=folder)
        except FileNotFoundError:
            pass
        os.close(folder)
    return {'updated': True}


def observe(config: BrokerConfig, path: str, session_id: str, base: str) -> dict:
    """Observe paths in trusted staging. Never load contributor Git config or execute filters."""
    import stat

    from ..group_paths import safe_path
    selected = private_path(config, path)
    if not SESSION.fullmatch(session_id) or not git.SHA.fullmatch(base):
        raise ValueError('Rynhult sent an invalid group session or commit.')
    target = selected / 'sessions' / session_id
    git.private_directory(target)
    source = target / '.git'
    git.private_directory(source)

    def read(parts, limit):
        folder = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=folder)
                os.close(folder)
                folder = child
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_size > limit or info.st_nlink != 1:
                    raise ValueError('This session\'s Git folder has links or unusual files, so Rynhult won\'t read it.')
                return os.read(fd, limit + 1)
            finally:
                os.close(fd)
        finally:
            os.close(folder)

    head = read(['HEAD'], 256).decode('ascii').strip()
    branch = None
    if head.startswith('ref: refs/heads/'):
        ref = head[5:]
        parts = ref.split('/')
        if not re.fullmatch(r'refs/heads/[A-Za-z0-9_./-]{1,180}', ref) or any(p in ('.', '..', '') for p in parts):
            raise ValueError('This session is on a branch name Rynhult won\'t read.')
        branch = ref[len('refs/heads/'):]
    with tempfile.TemporaryDirectory(prefix='acc-observe-') as scratch:
        repo = git.initialize(Path(scratch) / 'repo', 'observation')
        git.import_pack(repo, git.export(source, base))
        # The index is data, copied as a bounded regular file; the fresh repository
        # has no fsmonitor, external diff driver, attributes includes or hooks.
        try:
            (repo / 'index').write_bytes(read(['index'], 8 << 20))
        except FileNotFoundError:
            raise ValueError('This session\'s Git index is missing, so Rynhult can\'t see its changes.') from None
        tracked = git.run(repo, '-c', 'core.fsmonitor=false', '--work-tree', str(target),
                          'diff', '--name-only', '--no-ext-diff', '--no-textconv', '-z', base)
        untracked = git.run(repo, '-c', 'core.fsmonitor=false', '--work-tree', str(target),
                            'ls-files', '--others', '--exclude-standard', '-z')
    paths = []
    for raw in (tracked + untracked).split(b'\0'):
        if not raw:
            continue
        try:
            value = safe_path(raw.decode('utf-8'))
        except (ValueError, UnicodeError):
            continue
        if value not in paths:
            paths.append(value)
        if len(paths) >= 100:
            break
    return {'branch': branch, 'paths': paths, 'source': 'broker_git', 'truncated': len(paths) >= 100}
