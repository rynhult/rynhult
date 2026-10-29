#!/usr/bin/python3 -I
"""Owner-run provisioning for the dedicated image builder. No session privileges."""
import argparse
import grp
import os
import pwd
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

NAME = 'rynhult-build'
HOME = Path('/var/lib/rynhult-build')
SHELL = '/usr/sbin/nologin'
ETC = Path('/etc')
LINGER = Path('/var/lib/systemd/linger')
PATH = '/usr/sbin:/usr/bin:/sbin:/bin'
ENV = {'PATH': PATH, 'LANG': 'C', 'LC_ALL': 'C'}


class Refused(ValueError):
    pass


def ranges(text):
    result = []
    for line in text.splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        try:
            name, start, count = line.split(':')
            start, count = int(start), int(count)
            if not name or start < 0 or count <= 0 or start + count > 4294967295:
                raise ValueError
        except ValueError:
            raise Refused('invalid subordinate-ID entry; administrator must review /etc/subuid and /etc/subgid') from None
        result.append((name, start, count))
    return result


def allocate(subuid, subgid, uid=None, ids=()):
    """A subordinate range for the builder: never over another account's range, nor over a real user or group ID
    (`ids`): its containers would own files as that user (kit review BA-L1)."""
    tables = [ranges(subuid), ranges(subgid)]
    real = sorted(set(ids))
    names = {NAME, str(uid)} if uid is not None else {NAME}
    own = [[(start, count) for name, start, count in table if name in names] for table in tables]
    if any(own):
        if uid is None or len(own[0]) != 1 or own[0] != own[1]:
            raise Refused('existing subordinate ranges are incomplete or inconsistent; do not adopt them')
        start, count = own[0][0]
        if start < 100000 or count < 65536:
            raise Refused('existing subordinate range must start at 100000 or higher and contain at least 65536 IDs')
        if any(start < other + size and other < start + count
               for table in tables for name, other, size in table if name not in names):
            raise Refused('existing build subordinate range overlaps another account')
        if any(start <= value < start + count for value in real):
            raise Refused('existing build subordinate range covers a real user or group ID')
        return start, count
    if uid is not None:
        raise Refused('existing account lacks subordinate ranges; interrupted setup requires administrator review')
    start, count = 100000, 65536
    taken = [(start, count) for table in tables for _, start, count in table] + [(value, 1) for value in real]
    for other, size in sorted(taken):
        if start + count <= other:
            break
        if start < other + size:
            start = other + size
    if start + count > 4294967295:
        raise Refused('no free subordinate-ID range')
    return start, count


def plan(state):
    account = state.account
    selected = allocate(state.subuid, state.subgid, account.pw_uid if account else None,
                        {p.pw_uid for p in state.peers} | {g.gr_gid for g in state.groups})
    if account:
        primary = [g for g in state.groups if g.gr_gid == account.pw_gid]
        extras = [g for g in state.groups if NAME in g.gr_mem and g.gr_gid != account.pw_gid]
        shared = any(p.pw_name != NAME and (p.pw_uid == account.pw_uid or p.pw_gid == account.pw_gid)
                     for p in state.peers)
        if (not 0 < account.pw_uid <= state.system_max or not 0 < account.pw_gid <= state.system_max
                or account.pw_dir != str(HOME) or account.pw_shell != SHELL
                or len(primary) != 1 or primary[0].gr_name != NAME or extras or shared or state.password not in ('!', '!!', '*', '!*')
                or state.home != (account.pw_uid, account.pw_gid, 0o700)):
            raise Refused('existing rynhult-build has different identity, groups, password, shell or home properties; refusing to adopt/repair it')
        if state.cron or state.timers or state.manager:
            raise Refused('existing rynhult-build has a crontab, timers, queued jobs or active user manager; administrator must remove/review these first')
        return ([['loginctl', 'disable-linger', NAME]] if state.linger else []), selected
    if state.home is not None or any(g.gr_name == NAME for g in state.groups) or state.linger:
        raise Refused('orphaned build home/group/linger state; interrupted setup requires administrator review')
    start, count = selected
    return [
        ['useradd', '--system', '--user-group', '--no-create-home', '--home-dir', str(HOME), '--shell', SHELL,
         '--password', '!', '-K', 'SUB_UID_COUNT=0', '-K', 'SUB_GID_COUNT=0', NAME],
        ['usermod', '--add-subuids', f'{start}-{start + count - 1}', '--add-subgids', f'{start}-{start + count - 1}', NAME],
        ['install', '-d', '-m', '0700', '-o', NAME, '-g', NAME, str(HOME)],
        ['loginctl', 'disable-linger', NAME],
    ], selected


def tool(name):
    found = shutil.which(name, path=PATH)
    if not found:
        raise Refused(f'required tool missing: {name}; install prerequisites separately')
    return found


def query(command):
    return subprocess.run([tool(command[0]), *command[1:]], env=ENV, capture_output=True, text=True, timeout=30)


def execute(command):
    subprocess.run([tool(command[0]), *command[1:]], env=ENV, check=True, timeout=60)


def protected_parent():
    # Never repair a redirected/writable prefix as root.
    for parent in [HOME.parent, *HOME.parent.parents]:
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022
                or {'system.posix_acl_access', 'system.posix_acl_default'} & set(os.listxattr(parent))):
            raise Refused(f'unsafe build home parent: {parent}')


def inspect():
    try:
        account = pwd.getpwnam(NAME)
    except KeyError:
        account = None
    home = None
    if HOME.exists() or HOME.is_symlink():
        info = HOME.lstat()
        if (not stat.S_ISDIR(info.st_mode)
                or {'system.posix_acl_access', 'system.posix_acl_default'} & set(os.listxattr(HOME))):
            raise Refused('build home is linked, not a directory, or has ACLs; refusing to repair')
        home = (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode))
    defs = {}
    for line in (ETC / 'login.defs').read_text().splitlines():
        parts = line.split()
        if parts and not parts[0].startswith('#') and len(parts) > 1:
            defs[parts[0]] = parts[1]
    system_max = int(defs.get('SYS_UID_MAX', int(defs.get('UID_MIN', 1000)) - 1))
    password, cron, timers, manager = None, False, False, False
    if account:
        for line in (ETC / 'shadow').read_text().splitlines():
            parts = line.split(':')
            if parts[0] == NAME:
                password = parts[1]
        result = query(['crontab', '-u', NAME, '-l'])
        if result.returncode == 0:
            cron = True  # even an empty installed crontab is unexpected
        elif result.returncode != 1 or result.stderr.strip() != f'no crontab for {NAME}' or result.stdout.strip():
            raise Refused('cannot verify absence of build crontab')
        # atq prints all queued jobs as root; refuse this account's jobs only.
        if shutil.which('atq', path=PATH):
            result = query(['atq'])
            if result.returncode:
                raise Refused('cannot verify queued at jobs')
            cron |= any(line.split()[-1] == NAME for line in result.stdout.splitlines() if line.split())
        for folder in [HOME / '.config/systemd/user', HOME / '.local/share/systemd/user']:
            # Do not follow linked unit directories that could hide timers.
            for ancestor in [folder, folder.parent, folder.parent.parent]:
                if ancestor.is_symlink():
                    raise Refused('linked build user-unit directory')
            if folder.exists():
                timers |= any(p.is_symlink() or p.suffix == '.timer' for p in folder.rglob('*'))
        result = query(['systemctl', 'is-active', f'user@{account.pw_uid}.service'])
        if result.returncode == 0:
            manager = True
        elif result.returncode not in (3, 4) or result.stdout.strip() not in ('inactive', 'failed', 'unknown'):
            raise Refused('cannot verify inactive build user manager')
    return SimpleNamespace(account=account, groups=grp.getgrall(), password=password, home=home,
                           cron=cron, timers=timers, manager=manager,
                           linger=((LINGER / NAME).exists() or (LINGER / NAME).is_symlink()),
                           subuid=(ETC / 'subuid').read_text(), subgid=(ETC / 'subgid').read_text(),
                           system_max=system_max, peers=pwd.getpwall())


def prerequisites(commands):
    for name in {'crontab', 'systemctl', 'loginctl', *(command[0] for command in commands)}:
        tool(name)
    if not Path(SHELL).is_file():
        raise Refused(f'required nologin shell missing: {SHELL}')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true', help='inspect and print every proposed change, without applying')
    args = parser.parse_args(argv)
    try:
        if not args.dry_run and os.geteuid() != 0:
            raise Refused('apply requires root from the owner console; use --dry-run to inspect')
        protected_parent()
        commands, (start, count) = plan(inspect())
        prerequisites(commands)
        print(f'Subordinate UID/GID range: {start}-{start + count - 1} ({count} IDs each)')
        for command in commands:
            print(('DRY-RUN: ' if args.dry_run else 'APPLY: ') + shlex.join(command), flush=True)
            if not args.dry_run:
                execute(command)
        if not args.dry_run:
            remaining, _ = plan(inspect())
            if remaining:
                raise Refused('post-check still requires changes')
        print('Dry-run only; nothing changed.' if args.dry_run else 'Verified dedicated build account; never use it for sessions.')
        return 0
    except (Refused, OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f'setup-build-account: refused: {exc}. No automatic rollback; if changes began, retain this output and review the partial account before retrying.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
