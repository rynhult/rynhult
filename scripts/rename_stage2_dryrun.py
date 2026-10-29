#!/usr/bin/env python3
"""Read-only stage-2 proposal. No subprocesses, writes, service queries or imports of acc.

Use --root / for a live filesystem or --root PATH for an offline host tree.
Output is a conditional plan, not authorization or proof that a host is quiescent.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import stat
from pathlib import Path, PurePosixPath

ENV_KEY = re.compile(r'\bACC_[A-Z][A-Z0-9_]*\b')
ASSIGNMENT = re.compile(r'^\s*(?:export\s+)?((?:ACC|RYNHULT)_[A-Z][A-Z0-9_]*)=(.*)$')
MAX_BYTES = 1024 * 1024
UNIT_DIRS = ('/etc/systemd/system', '/usr/lib/systemd/system', '/lib/systemd/system',
             '/etc/systemd/user', '/usr/lib/systemd/user')
NOTICE = ('PROPOSAL ONLY: no changes made. Filesystem evidence only; loaded jobs, process environments, '
          'tmux sessions, ACLs, mounts and Windows registry/tasks require separate operator verification. '
          'Missing files do not prove absence of an installation. See docs/RENAME-STAGE-2.md.')


def renamed(name):
    if name == 'accd' or name.startswith('accd.'):
        return 'rynhultd' + name[4:]
    if name == 'acc' or name.startswith(('acc-', 'acc.')):
        return 'rynhult' + name[3:]
    if name.startswith('local.acc.'):
        return name.replace('local.acc.', 'local.rynhult.', 1)
    if name == 'ACC':
        return 'Rynhult'
    if name == '.acc':
        return '.rynhult'
    return name


class Planner:
    def __init__(self, root):
        self.root = root
        self.rows = []
        self.seen = set()

    def add(self, action, source, target='', detail='', **extra):
        row = dict(action=action, source=source, target=target, detail=detail, **extra)
        if row not in self.rows:
            self.rows.append(row)

    def path(self, logical):
        """Never traverse links, including intermediate directories in an offline tree."""
        current = self.root
        parts = PurePosixPath(logical).parts[1:]
        for index, part in enumerate(parts):
            current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                return None
            except OSError:
                self.add('blocked', logical, detail='Cannot stat path; inspection incomplete.')
                return None
            if stat.S_ISLNK(info.st_mode):
                self.add('blocked', '/' + '/'.join(parts[:index + 1]),
                         detail='Symlink not followed; verify existing compatibility link manually.')
                return None
        return current

    def children(self, logical):
        path = self.path(logical)
        if path is None:
            return []
        try:
            if not path.is_dir():
                self.add('blocked', logical, detail='Expected a directory.')
                return []
            return sorted(p.name for p in path.iterdir())
        except OSError:
            self.add('blocked', logical, detail='Cannot list directory; inspection incomplete.')
            return []

    def text(self, logical):
        path = self.path(logical)
        if path is None:
            return None
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                self.add('blocked', logical, detail='Not a regular file or exceeds 1 MiB; not read.')
                return None
            # O_NOFOLLOW/O_NONBLOCK also guard a final-component swap to a link/FIFO.
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    self.add('blocked', logical, detail='File type changed; not read.')
                    return None
                data = stream.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                self.add('blocked', logical, detail='File grew beyond 1 MiB; not parsed.')
                return None
            return data.decode('utf-8')
        except (OSError, UnicodeError):
            self.add('blocked', logical, detail='Cannot read UTF-8 file; inspection incomplete.')
            return None

    def compat_path(self, old, target=None, action='compat-path'):
        if self.path(old) is None:
            return
        target = target or str(PurePosixPath(old).with_name(renamed(PurePosixPath(old).name)))
        new = self.path(target)
        if new is not None:
            self.add('blocked', old, target, 'Both names exist; never merge, overwrite or choose by timestamp.')
        else:
            detail = ('Keep existing object in place; propose new-name alias only after ownership, '
                      'mount and process checks. No move while sessions run.')
            if action == 'launchd-cutover':
                detail = ('Deferred: drain node, unload old label, render new plist/label, load once; '
                          'launchd has no systemd-style aliases. Preserve enrollment and old plist for rollback.')
            elif action.startswith('windows-'):
                detail = ('Deferred: inspect task/installer ownership with native Windows tools; preserve WSL distro '
                          'and task action. Never register duplicate keep-alive tasks or move Program Files live.')
            self.add(action, old, target, detail)

    def env_file(self, logical):
        if logical in self.seen:
            return
        self.seen.add(logical)
        text = self.text(logical)
        if text is None:
            return
        assignments = {}
        for number, line in enumerate(text.splitlines(), 1):
            match = ASSIGNMENT.match(line)
            if match:
                key, raw = match.groups()
                try:
                    value = shlex.split(raw, comments=True)
                except ValueError:
                    self.add('blocked', logical, detail='Malformed assignment; values withheld.', line=number)
                    continue
                if key in assignments:
                    self.add('blocked', logical, detail='Duplicate assignment; resolve manually.', identifier=key,
                             line=number)
                assignments[key] = (' '.join(value), number)
        for key, (value, number) in sorted(assignments.items()):
            if not key.startswith('ACC_'):
                continue
            target = 'RYNHULT_' + key[4:]
            action = 'env-fallback'
            detail = 'Add new key with identical value; retain old key for older readers. Values withheld.'
            if target in assignments:
                action = 'keep' if assignments[target][0] == value else 'blocked'
                detail = ('Both keys agree; retain both.' if action == 'keep' else
                          'Old/new values disagree; resolve before any migration. Values withheld.')
            self.add(action, logical, target, detail, identifier=key, line=number)
        # Units, plists and shell snippets may carry keys outside ordinary assignments.
        for number, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith('#'):
                continue
            if not ASSIGNMENT.match(line):
                for key in sorted(set(ENV_KEY.findall(line))):
                    self.add('review-reference', logical, 'RYNHULT_' + key[4:],
                             'Embedded reference: render dual-compatible configuration; never text-replace secrets.',
                             identifier=key, line=number)
            if '/srv/acc' in line or '/etc/acc' in line or '/run/acc' in line or '/var/lib/acc' in line:
                self.add('keep-path-reference', logical, detail='Legacy path reference retained during compatibility '
                         'phase; switch only after alias validation. Values withheld.', line=number)

    def units(self, directory):
        for name in self.children(directory):
            logical = directory + '/' + name
            if name.startswith(('rynhult-', 'rynhultd.')) and name.endswith('.d'):
                self.add('blocked', logical, detail='Review new-name drop-ins before adding aliases.')
            if (name.startswith('rynhult-') and '@' in name and '@.' not in name
                    and name.endswith(('.service', '.socket'))):
                parent = self.path(directory)
                canonical = 'acc' + name[7:]
                template = canonical.split('@', 1)[0] + '@.' + canonical.rsplit('.', 1)[1]
                old = directory + '/' + template
                source = self.path(old)
                alias = parent / name if parent else None
                if (alias is not None and alias.is_symlink() and os.readlink(alias) in (template, old)
                        and source is not None and stat.S_ISREG(source.lstat().st_mode)
                        and not os.path.lexists(str(alias) + '.d')):
                    self.add('unit-alias-present', old, logical,
                             'Verified instance alias to the legacy template; same instance and job.')
                else:
                    self.add('blocked', logical, old,
                             'Custom, masked or unverified instance alias; manual review required.')
            if name.startswith(('acc-', 'accd.')):
                if name.endswith(('.service', '.timer', '.path', '.socket')):
                    parent = self.path(directory)
                    target = directory + '/' + renamed(name)
                    alias = parent / renamed(name) if parent else None
                    source = self.path(logical)
                    if (alias is not None and alias.is_symlink() and os.readlink(alias) in (name, logical)
                            and source is not None and stat.S_ISREG(source.lstat().st_mode)
                            and not os.path.lexists(str(alias) + '.d')):
                        self.add('unit-alias-present', logical, target,
                                 'Verified alias to the legacy unit; one job/listener. '
                                 'Capability required for fleet cleanup: rename-compat-1.')
                    else:
                        self.compat_path(logical, action='unit-alias')
                    self.env_file(logical)
                elif name.endswith('.d'):
                    for child in self.children(logical):
                        if child.endswith('.conf'):
                            self.env_file(logical + '/' + child)
            if name.endswith(('.wants', '.requires')):
                for child in self.children(logical):
                    if child.startswith(('acc-', 'accd.')):
                        self.add('review-enablement', logical + '/' + child,
                                 detail='Retain dependency/enablement link; systemd alias is not a second daemon.')

    def scan(self, extras):
        # Bounded discovery: do not walk application data, workspaces, secrets or databases.
        for directory in ('/srv', '/etc', '/etc/default', '/run', '/var/run', '/var/lib',
                          '/var/backups', '/var/log', '/opt', '/usr/local/bin', '/usr/local/sbin', '/usr/local/lib', '/etc/caddy'):
            for name in self.children(directory):
                if name == 'acc' or name.startswith(('acc-', 'accd')):
                    self.compat_path(directory + '/' + name)
        for directory in UNIT_DIRS:
            self.units(directory)
        for directory in ('/etc/default', '/etc/acc/broker', '/etc/rynhult/broker'):
            for name in self.children(directory):
                if directory.endswith('/broker') or name.startswith(('acc', 'rynhult')):
                    self.env_file(directory + '/' + name)
        for logical in ('/etc/acc-node.env', '/etc/rynhult-node.env', *extras):
            if logical in extras and self.path(logical) is None:
                self.add('blocked', logical, detail='Explicit input missing or inaccessible; inspection incomplete.')
            self.env_file(logical)
        for base in ('/home', '/Users'):
            for user in self.children(base):
                home = base + '/' + user
                for suffix, target in (('/.acc', '/.rynhult'),
                                       ('/Library/Application Support/ACC', '/Library/Application Support/Rynhult'),
                                       ('/Library/Logs/ACC', '/Library/Logs/Rynhult')):
                    self.compat_path(home + suffix, home + target)
                for suffix in ('/.acc/acc-node.env', '/Library/Application Support/ACC/acc-node.env'):
                    self.env_file(home + suffix)
                agents = home + '/Library/LaunchAgents'
                for name in self.children(agents):
                    if name.startswith('local.acc.') and name.endswith('.plist'):
                        logical = agents + '/' + name
                        self.compat_path(logical, action='launchd-cutover')
                        self.env_file(logical)
        for base in ('/ProgramFiles', '/Program Files', '/ProgramData'):
            self.compat_path(base + '/ACC', base + '/Rynhult', action='windows-review')
        for name in ('ACC (WSL)', 'ACC node (WSL)'):
            self.compat_path('/Windows/System32/Tasks/' + name,
                             '/Windows/System32/Tasks/' + name.replace('ACC', 'Rynhult'),
                             action='windows-task-cutover')
        for logical in ('/var/lib/caddy/acc-access.json', '/srv/acc/app/.acc-build'):
            if self.path(logical) is not None:
                self.add('keep', logical, logical, 'Preserve historical log/build identity and existing readers.')
        for logical in ('/srv/acc/data/acc.db', '/srv/rynhult/data/acc.db'):
            if self.path(logical) is not None:
                self.add('keep', logical, logical, 'Keep database basename, WAL/SHM and backup member names; '
                         'never open or rename a live database.')
        return sorted(self.rows, key=lambda r: (r['source'], r.get('line', 0), r['action'], r['target']))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True, help='Host filesystem root (explicit / for live host)')
    parser.add_argument('--env-file', action='append', default=[], help='Additional absolute POSIX host path; repeatable')
    parser.add_argument('--json', action='store_true', help='Emit stable structured findings without values')
    args = parser.parse_args()
    if args.root.is_symlink() or not args.root.is_dir():
        parser.error('--root must be an existing directory, not a symlink')
    for name in args.env_file:
        if not name.startswith('/') or '..' in PurePosixPath(name).parts:
            parser.error('--env-file must be an absolute host path without ..')
    planner = Planner(args.root.absolute())
    rows = planner.scan(args.env_file)
    blocked = any(row['action'] == 'blocked' for row in rows)
    if args.json:
        print(json.dumps(dict(schema_version=1, notice=NOTICE, incomplete=blocked, findings=rows), indent=2))
    else:
        print(NOTICE)
        for row in rows:
            location = row['source'] + (f":{row['line']}" if 'line' in row else '')
            identifier = f" [{row['identifier']}]" if 'identifier' in row else ''
            print(f"{row['action']}: {location}{identifier} -> {row['target']}\n  {row['detail']}")
        if not rows:
            print('No detected legacy identifiers in inspected locations.')
    return 2 if blocked else 0


if __name__ == '__main__':
    raise SystemExit(main())
