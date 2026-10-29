#!/usr/bin/python3 -I
"""Phase B: add public aliases, or remove only journaled additions after consumers drain.

No service operations, config rewrites, path migrations or privilege policy changes.
All host access is relative to protected directory descriptors, including offline tests.
"""
from __future__ import annotations

import argparse
import fcntl
import grp
import json
import os
import runpy
import stat
import sys
from pathlib import Path

UNIT_API = runpy.run_path(str(Path(__file__).with_name('unit-aliases.py')))
JOURNAL = '/etc/rynhult-phase-b.json'
HOOKS = ('hook', 'guard', 'mcp-team', 'statusline', 'request', 'mcp-github', 'wiki-check')
HELPERS = ('agent-add', 'agent-share', 'update', 'packages', 'publish', 'hoststate', 'backup',
           'accounts', 'root-session', 'containers', 'claude-apt-repo')


def command_aliases(role, *, mac_prefix=None, mac_bin=None):
    if role == 'macos':
        if not mac_prefix or not mac_bin:
            raise ValueError('Mac aliases require the existing app and public bin paths')
        pairs = [(f'{mac_bin}/rynhult', f'{mac_prefix}/app/.venv/bin/acc'),
                 (f'{mac_bin}/rynhultd', f'{mac_prefix}/app/.venv/bin/accd'),
                 (f'{mac_bin}/rynhult-node', f'{mac_prefix}/bin/acc-node')]
        for suffix in HOOKS:
            target = f'{mac_prefix}/bin/acc-{suffix}'
            pairs.extend(((f'{mac_prefix}/bin/rynhult-{suffix}', target), (f'{mac_bin}/rynhult-{suffix}', target)))
        for suffix in ('restore', 'db-transfer'):
            pairs.append((f'{mac_bin}/rynhult-{suffix}', f'{mac_prefix}/app/.venv/bin/acc-{suffix}'))
        return pairs
    if role not in ('main', 'node'):
        raise ValueError('unknown alias role')
    prefix = '/srv/acc' if role == 'main' else '/opt/acc'
    pairs = [('/usr/local/bin/rynhult', f'{prefix}/app/.venv/bin/acc'),
             ('/usr/local/bin/rynhultd', f'{prefix}/app/.venv/bin/accd'),
             ('/usr/local/bin/rynhult-node', '/usr/local/bin/acc-node' if role == 'node'
              else f'{prefix}/app/.venv/bin/acc-node')]
    for suffix in HOOKS:
        target = f'{prefix}/bin/acc-{suffix}'
        pairs.extend(((f'{prefix}/bin/rynhult-{suffix}', target), (f'/usr/local/bin/rynhult-{suffix}', target)))
    for suffix in HELPERS:
        pairs.append((f'/usr/local/sbin/rynhult-{suffix}', f'/usr/local/sbin/acc-{suffix}'))
    for suffix in ('restore', 'db-transfer'):
        pairs.append((f'/usr/local/bin/rynhult-{suffix}', f'{prefix}/app/.venv/bin/acc-{suffix}'))
    # Existing stage-1 names are left to their original installers. These are
    # legacy executables, not parallel Python namespaces or copied helpers.
    pairs.append(('/usr/local/bin/rynhult-rest-server', '/usr/local/bin/acc-rest-server'))
    return pairs


class Host:
    def __init__(self, root, *, journal_path=JOURNAL, user_mode=False):
        self.journal_path = journal_path
        self.user_mode = user_mode
        self.admin_gid = None
        if user_mode and sys.platform == 'darwin':
            try:
                self.admin_gid = grp.getgrnam('admin').gr_gid
            except KeyError:
                pass  # No known admin group: retain the strict permission check.
        self.owner = 0 if Path(root) == Path('/') else os.geteuid()
        self.fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self.check(os.fstat(self.fd))

    def check(self, info):
        owners = {self.owner, os.geteuid()} if self.user_mode else {self.owner}
        sticky = self.user_mode and stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and info.st_mode & stat.S_ISVTX
        # Homebrew's user-owned bin is normally admin-writable on macOS. Admin
        # members already have sudo; this exception never applies to files or to
        # directories owned by another user, and never permits world writes.
        admin_directory = (self.admin_gid is not None and stat.S_ISDIR(info.st_mode)
                           and info.st_uid == os.geteuid() and info.st_gid == self.admin_gid
                           and not info.st_mode & 0o002)
        if info.st_uid not in owners or (info.st_mode & 0o022 and not (sticky or admin_directory)):
            raise ValueError('untrusted alias path: wrong owner or writable by others')

    def parent(self, path):
        parts = path.split('/')
        if not path.startswith('/') or any(p in ('.', '..', '') for p in parts[1:]):
            raise ValueError('invalid alias path')
        fd = os.dup(self.fd)
        try:
            for part in parts[1:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
                self.check(os.fstat(fd))
            return fd, parts[-1]
        except BaseException:
            os.close(fd)
            raise

    def info(self, path):
        try:
            fd, name = self.parent(path)
        except FileNotFoundError:
            return None
        try:
            try:
                return os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                return None
        finally:
            os.close(fd)

    def link(self, path):
        fd, name = self.parent(path)
        try:
            return os.readlink(name, dir_fd=fd)
        finally:
            os.close(fd)

    def mutate(self, path, target=None):
        fd, name = self.parent(path)
        try:
            if target is None:
                os.unlink(name, dir_fd=fd)
            else:
                os.symlink(target, name, dir_fd=fd)
            os.fsync(fd)
        finally:
            os.close(fd)

    def journal(self, value=None):
        fd, name = self.parent(self.journal_path)
        try:
            if value is None:
                try:
                    file = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                except FileNotFoundError:
                    return None
                with os.fdopen(file) as stream:
                    info = os.fstat(stream.fileno())
                    self.check(info)
                    if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
                        raise ValueError('unsafe phase B journal')
                    return json.load(stream)
            # Atomic, durable checkpoint before creating links. A crash resumes
            # by inspecting the recorded intent and actual link, never replaying installs.
            temp = name + '.tmp'
            file = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
            try:
                with os.fdopen(file, 'w') as stream:
                    json.dump(value, stream, sort_keys=True)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.rename(temp, name, src_dir_fd=fd, dst_dir_fd=fd)
                os.fsync(fd)
            finally:
                try:
                    os.unlink(temp, dir_fd=fd)
                except FileNotFoundError:
                    pass
        finally:
            os.close(fd)


def reconcile(root, role, *, dry_run=False, rollback=False, mac_prefix=None, mac_bin=None):
    commands = command_aliases(role, mac_prefix=mac_prefix, mac_bin=mac_bin)
    journal_path = f'{mac_prefix}/rename-phase-b.json' if role == 'macos' else JOURNAL
    host = Host(root, journal_path=journal_path, user_mode=role == 'macos')
    lock = None
    try:
        # Lock the protected directory itself: no lock file to create in a dry run
        # or remove unsafely while another installer waits on its inode.
        lock, _ = host.parent(journal_path)
        if not dry_run:
            fcntl.flock(lock, fcntl.LOCK_EX)
        units = [(f'/etc/systemd/system/rynhult{name[3:]}', f'/etc/systemd/system/{name}')
                 for name in UNIT_API['UNITS']] if role != 'macos' else []
        allowed = dict(commands + units)
        if not rollback and role != 'macos':
            dashboard = host.info('/etc/systemd/system/accd.service')
            node = host.info('/etc/systemd/system/acc-node.service')
            if (role == 'node' and dashboard is not None) or (role == 'main' and node is not None and dashboard is None):
                raise ValueError('host role conflict: use the installer for this existing main/node')
        state = host.journal() if host.info(journal_path) is not None else {'schema': 1, 'role': role, 'links': {}}
        if (not isinstance(state, dict) or state.get('schema') != 1 or state.get('role') != role
                or not isinstance(state.get('links'), dict)):
            raise ValueError('invalid phase B journal or role conflict')
        for name, target in state['links'].items():
            if name not in allowed or target != os.path.relpath(allowed[name], os.path.dirname(name)):
                raise ValueError('unrecognized phase B journal entry')
        pending = []
        if not rollback:
            # Reuse reviewed phase A unit preflight, including vendor/mask/drop-in
            # checks; parent traversal is separately checked without following links.
            for name, _ in units:
                host.info(name)
            directory = Path(root) / 'etc/systemd/system'
            if units and directory.exists():
                UNIT_API['install_aliases'](directory, check_only=True,
                    search_dirs=[Path(root) / p.lstrip('/') for p in UNIT_API['SEARCH_DIRS']],
                    logical_directory=Path('/etc/systemd/system'))
            for name, target in commands + units:
                source = host.info(target)
                if source is None:
                    continue  # Optional component not installed: do not create dangling aliases.
                host.check(source)
                if not stat.S_ISREG(source.st_mode):
                    raise ValueError(f'alias target is not a regular file: {target}')
                info = host.info(name)
                relative = os.path.relpath(target, os.path.dirname(name))
                if info is not None:
                    accepted = (relative, target)
                    public = mac_bin if role == 'macos' else '/usr/local/bin'
                    if name in tuple(f'{public}/{n}' for n in ('rynhult', 'rynhultd', 'rynhult-node')):
                        prefix = mac_prefix if role == 'macos' else '/srv/acc' if role == 'main' else '/opt/acc'
                        stage_one = f'{prefix}/app/.venv/bin/{Path(name).name}'
                        alternate = (stage_one, os.path.relpath(stage_one, os.path.dirname(name)))
                        if stat.S_ISLNK(info.st_mode) and host.link(name) in alternate:
                            original = host.info(stage_one)
                            if original is None or not stat.S_ISREG(original.st_mode):
                                raise ValueError(f'alias target is not a regular file: {stage_one}')
                            host.check(original)
                        accepted += alternate
                    if stat.S_ISLNK(info.st_mode) and host.link(name) in accepted:
                        continue
                    raise ValueError(f'alias conflict: {name}')
                # Validate destination parent before journaling any change.
                fd, _ = host.parent(name)
                os.close(fd)
                pending.append((name, relative))
        else:
            for name, target in state['links'].items():
                info = host.info(name)
                if info is not None:
                    if not stat.S_ISLNK(info.st_mode) or host.link(name) != target:
                        raise ValueError(f'rollback alias changed: {name}')
                    pending.append((name, target))
        for name, target in pending:
            print(f'{"remove" if rollback else "alias"} {name} -> {target}')
        if dry_run:
            return
        if rollback:
            for name, _ in pending:
                host.mutate(name)
            if host.info(journal_path) is not None:
                host.mutate(journal_path)
        elif pending:
            state['links'].update(pending)
            host.journal(state)
            for name, target in pending:
                host.mutate(name, target)
    finally:
        if lock is not None:
            os.close(lock)
        os.close(host.fd)


def capture_running(root):
    """Read-only probe: never create a lock or signal a process during deployment planning."""
    # Only staging excludes updates. The worker lease also covers scheduling,
    # DB waits and uploads; none is a reason for a read-only dry run to refuse.
    for relative in ('srv/acc/data/.capture-update.lock',):
        try:
            fd = os.open(root / relative, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            continue
        except PermissionError:
            # Only root (the real install's preflight) can see Rynhult's private data folder.
            print("note: can't check for a running backup capture without root; the real install checks it",
                  file=sys.stderr)
            return False
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('unsafe backup capture lease')
        finally:
            os.close(fd)
        held = lease_held(info)
        if held is None:
            print("note: can't read /proc/locks to check for a running backup capture; the real install checks it",
                  file=sys.stderr)
            return False
        if held:
            return True
    return False


def lease_held(info) -> bool | None:
    """Whether any process holds a flock on this file, from the kernel's lock table: never taking the lease, so a
    dry run can't block or interleave with a capture or the backup worker (security review 9, SR9-12). None when the
    table can't be read."""
    try:
        table = Path('/proc/locks').read_text()
    except OSError:
        return None
    # "1: FLOCK  ADVISORY  WRITE 4242 fc:05:77229842 0 EOF": device major:minor in hex, then the inode. A waiting
    # request ("1: -> FLOCK ...") also means a holder exists.
    target = f'{os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}:{info.st_ino}'
    return any(' FLOCK ' in f' {line} ' and target in line.split() for line in table.splitlines())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--role', choices=('main', 'node', 'macos', 'auto'), required=True)
    parser.add_argument('--aliases-only', action='store_true', help='Only additive phase B operations')
    parser.add_argument('--dry-run', action='store_true', help='Read-only phase B plan, not a full install simulation')
    parser.add_argument('--rollback-aliases', action='store_true', help='Remove journaled links after consumers drain')
    parser.add_argument('--alias-root', type=Path, default=Path('/'), help='Offline root; dry-run only')
    parser.add_argument('--mac-prefix', help='Existing Mac user app directory; no path migration')
    parser.add_argument('--mac-bin', help='Existing Homebrew bin directory')
    args = parser.parse_args()
    if args.alias_root != Path('/') and not args.dry_run:
        parser.error('--alias-root requires --dry-run')
    # Read-only inspection is safe under a root-run qualification suite too.
    # Keep both installation and rollback restricted to the signed-in user.
    if not args.dry_run and args.role == 'macos' and os.geteuid() == 0:
        parser.error('Mac aliases must run as the signed-in user, not root')
    if not args.dry_run and args.role != 'macos' and os.geteuid() != 0:
        parser.error('alias installation requires root')
    try:
        if args.dry_run and args.role == 'main' and capture_running(args.alias_root):
            parser.exit(75, 'A backup capture is running or other backup work is active; try again shortly.\n')
        if args.dry_run:
            print('Phase B dry run: aliases only; no changes or service operations')
        if args.role == 'auto':
            if not args.rollback_aliases:
                parser.error('--role auto is only for journaled rollback')
            inspection = Host(args.alias_root)
            try:
                if inspection.info(JOURNAL) is None:
                    return
                state = inspection.journal()
            finally:
                os.close(inspection.fd)
            if not isinstance(state, dict) or state.get('role') not in ('main', 'node'):
                raise ValueError('invalid phase B journal role')
            args.role = state['role']
        reconcile(args.alias_root, args.role, dry_run=args.dry_run, rollback=args.rollback_aliases,
                  mac_prefix=args.mac_prefix, mac_bin=args.mac_bin)
    except (OSError, ValueError) as error:
        parser.exit(1, f'phase B preflight failed: {error}\n')


if __name__ == '__main__':
    main()
