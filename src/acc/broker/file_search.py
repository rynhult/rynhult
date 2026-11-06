"""Bounded filename discovery; never execute Git, search contents, or follow links."""
from __future__ import annotations

import fnmatch
import os
import stat
import sys
import time
import unicodedata
from pathlib import Path

from .files import _open_dir, protected, workspace
from .ops import OpError

MAX_VISITED = 10_000
MAX_RESULTS = 100
MAX_SECONDS = 1.0
MAX_DEPTH = 32
MAX_IGNORE_BYTES = 8192
MAX_IGNORE_PATTERNS = 256
EXCLUDED = frozenset({'.git', 'node_modules'})


def mount_identity(fd):
    if sys.platform == 'linux':
        from acc.folder_policy import FolderError, mount_id
        try:
            return mount_id(fd)
        except FolderError as error:
            raise OpError(str(error)) from None
    return os.fstat(fd).st_dev


def checked_directory(config, fd):
    if not config.folder_revision:
        return
    from acc.folder_policy import FolderError, broker_path
    if sys.platform != 'linux':
        raise OpError('Reviewed folder checks are unavailable on this machine.')
    location = os.readlink(f'/proc/self/fd/{fd}')
    try:
        broker_path(config, location)
    except FolderError as error:
        raise OpError(str(error)) from None
    actual = Path(location)
    if location.endswith(' (deleted)') or protected(config, actual, ''):
        raise OpError('The checked folder was removed or moved into a protected location.')
    if config.allowed_folders and not any(actual == root or root in actual.parents for root in config.allowed_roots):
        raise OpError('The checked folder moved outside the allowed roots.')


def validate(q, limit):
    if (not isinstance(q, str) or not 1 <= len(q) <= 200 or not q.strip()
            or any(ord(c) < 32 or ord(c) == 127 for c in q) or '\\' in q
            or any(p in {'.', '..'} for p in q.split('/'))):
        raise OpError('Enter 1–200 search characters without control characters or . / .. path components.')
    if type(limit) is not int or not 1 <= limit <= MAX_RESULTS:
        raise OpError('Choose a result limit between 1 and 100.')
    return unicodedata.normalize('NFC', q.strip()).casefold()


def rank(query, name, path):
    name, path = (unicodedata.normalize('NFC', value).casefold() for value in (name, path))
    if query in name:
        return 0
    if query in path:
        return 1
    chars = iter(path)
    return 2 if all(any(c == wanted for c in chars) for wanted in query) else None


def ignored(path, directory, rules):
    answer = False
    for base, pattern, negate, only_directory, anchored in rules:
        if only_directory and not directory:
            continue
        relative = path[len(base):]
        candidate = relative if anchored or '/' in pattern else relative.rsplit('/', 1)[-1]
        if fnmatch.fnmatchcase(candidate, pattern):
            answer = not negate
    return answer


def search(config, path, q, limit=20):
    query = validate(q, limit)
    start = time.monotonic()
    results = []
    visited = 0
    pattern_count = 0
    reason = None

    class Stop(Exception):
        pass

    class SkipSubtree(Exception):
        pass

    def budget():
        nonlocal reason
        if time.monotonic() - start >= MAX_SECONDS:
            reason = 'time_limit'
            raise Stop

    def rules_at(fd, base, inherited):
        nonlocal pattern_count, reason
        budget()
        try:
            handle = os.open('.gitignore', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        except FileNotFoundError:
            return inherited
        except OSError:
            reason = reason or 'unreadable'
            raise SkipSubtree  # unknown exclusions: do not enumerate this subtree
        try:
            info = os.fstat(handle)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                reason = 'ignore_limit'
                raise SkipSubtree
            data = os.read(handle, MAX_IGNORE_BYTES + 1)
        finally:
            os.close(handle)
        if len(data) > MAX_IGNORE_BYTES:
            reason = 'ignore_limit'
            raise SkipSubtree
        rules = list(inherited)
        for line in data.decode('utf-8', errors='replace').splitlines():
            budget()
            line = line.rstrip()
            if not line or line.startswith('#'):
                continue
            negate = line.startswith('!')
            pattern = line[1:] if negate else line
            anchored, directory = pattern.startswith('/'), pattern.endswith('/')
            pattern = pattern.strip('/')
            if not pattern:
                continue
            pattern_count += 1
            if pattern_count > MAX_IGNORE_PATTERNS:
                reason = 'ignore_limit'
                raise Stop
            rules.append((base, pattern, negate, directory, anchored))
        return rules

    with workspace(config, path) as (root, root_fd):
        root_mount = mount_identity(root_fd)

        def walk(fd, components, inherited):
            nonlocal visited, reason
            checked_directory(config, fd)
            prefix = '/'.join(components)
            try:
                rules = rules_at(fd, prefix + '/' if prefix else '', inherited)
            except SkipSubtree:
                return
            try:
                with os.scandir(fd) as entries:
                    for entry in entries:
                        budget()
                        if visited >= MAX_VISITED:
                            reason = 'entry_limit'
                            raise Stop
                        visited += 1
                        checked_directory(config, fd)
                        rel = '/'.join([*components, entry.name])
                        if len(rel) > 4096:
                            reason = reason or 'path_limit'
                            continue
                        try:
                            rel.encode('utf-8')
                        except UnicodeError:
                            reason = reason or 'unsupported_name'
                            continue
                        if entry.name in EXCLUDED or protected(config, root, rel):
                            continue
                        try:
                            info = entry.stat(follow_symlinks=False)
                            directory = stat.S_ISDIR(info.st_mode)
                            if ignored(rel, directory, rules):
                                continue
                            if directory:
                                if len(components) >= MAX_DEPTH:
                                    reason = reason or 'depth_limit'
                                    continue
                                # Every descent stays relative to the held parent; symlink swaps refuse.
                                child = _open_dir(fd, [entry.name])
                                try:
                                    if mount_identity(child) != root_mount:
                                        reason = reason or 'mount_boundary'
                                        continue
                                    walk(child, [*components, entry.name], rules)
                                finally:
                                    os.close(child)
                            elif stat.S_ISREG(info.st_mode):
                                score = rank(query, entry.name, rel)
                                if score is not None:
                                    if len(results) >= limit:
                                        reason = 'result_limit'
                                        raise Stop
                                    results.append((score, {'name': entry.name, 'path': rel, 'kind': 'file'}))
                        except OSError:
                            reason = reason or 'unreadable'
            except OSError:
                reason = reason or 'unreadable'

        try:
            walk(root_fd, [], [])
        except Stop:
            pass
    results.sort(key=lambda row: (row[0], row[1]['path'].casefold(), row[1]['path']))
    return {'results': [row for _, row in results], 'visited': visited, 'truncated': reason is not None, 'reason': reason}
