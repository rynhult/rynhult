#!/usr/bin/python3 -I
"""The session image build's quiet-account check (security review 8, SR8-3, option B).

The image is built as an ordinary account, and any other process of that account can write the build's files: the
throwaway store's layers, the build context, the saved archive. So deploy/containers/build-image runs this before it
starts (`start`: settings and census), before `podman save` and before it writes the checksum the owner pins
(`save` and `checksum`: census). Each refuses with every foreign process named.

The census reads /proc, like the qualification kit's: every process whose real, effective, saved or filesystem UID is
the account's, or in the account's subordinate range (/etc/subuid: a process in a user namespace of the account), is
attributed to the build only if it descends from the build's shell. One exception: Podman's pause process, which
double-forks out of the build. It is allowed only when /proc/PID/exe is the system's catatonit, its command line is
exactly `catatonit -P` (what Podman runs; comm and cmdline alone are the account's to choose, the exe link isn't), and
it started after the build's shell. The build gives Podman a tmp dir of its own, so its pause process is always a new
one; an older catatonit could have had code injected by a tracing parent (kit review H-1). It only holds the user
namespace; anything that joins that namespace is a process of its own, and counted.

A process that keeps forking and exiting is never at one PID long enough to be read from /proc (kit review H-2). The
kernel's task count can't be dodged that way: the account's user slice and its broker service must hold no task the
scan didn't account for, and the scan runs three times.

Each foreign process is named by PID, a fixed label and its escaped cgroup, never by text the process chose. A process
that starts and ends entirely between two checks isn't seen; the settings check removes the usual ways one starts
unattended (lingering, user timers, cron and at). Standard library only.
"""
import os
import pwd
import subprocess
import sys
import time
from pathlib import Path

CATATONIT = ('/usr/bin/catatonit', '/usr/libexec/podman/catatonit')
SHOWN = frozenset(b'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/@._:-')
LIMIT = 8
SCANS, SCAN_GAP = 3, 0.05


def shown(text, limit=160):
    """Plain path characters only; anything else escaped (a backslash too), bounded."""
    out = ''.join(chr(b) if b in SHOWN else f'\\x{b:02x}' for b in text.encode(errors='surrogateescape'))
    return out if len(out) <= limit else out[:limit] + '...'


def subordinate(name, uid, path=Path('/etc/subuid')):
    """The account's subordinate UID ranges, as ranges."""
    ranges = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return ranges
    for line in lines:
        fields = line.strip().split(':')
        if len(fields) == 3 and fields[0] in (name, str(uid)) and fields[1].isdigit() and fields[2].isdigit():
            ranges.append(range(int(fields[1]), int(fields[1]) + int(fields[2])))
    return ranges


def process_table(proc):
    """{pid: (parent, uids, cgroup, zombie, started, threads)}; None when /proc can't be listed. `started` is the start
    time in clock ticks since boot (None when unreadable), `threads` the task count the kernel's pids counter sees."""
    try:
        entries = [entry.name for entry in proc.iterdir() if entry.name.isdigit()]
    except OSError:
        return None
    table = {}
    for pid in entries:
        try:
            status = dict(line.split(':', 1) for line in (proc / pid / 'status').read_text().splitlines() if ':' in line)
            uids = {int(value) for value in status['Uid'].split()}
            parent = int(status['PPid'].strip())
            zombie = status.get('State', '').strip().startswith('Z')
            cgroup = next((line[3:] for line in (proc / pid / 'cgroup').read_text().splitlines()
                           if line.startswith('0::')), '')
            threads = int(status.get('Threads', '1').strip() or 1)
        except (OSError, KeyError, ValueError):
            continue  # exited while being read
        try:  # field 22, after the parenthesised command name (which may hold spaces or parentheses)
            started = int((proc / pid / 'stat').read_text().rsplit(')', 1)[1].split()[19])
        except (OSError, IndexError, ValueError):
            started = None
        table[int(pid)] = (parent, uids, cgroup.strip(), zombie, started, threads)
    return table


def is_pause_process(proc, pid):
    try:
        return (os.readlink(proc / str(pid) / 'exe') in CATATONIT
                and (proc / str(pid) / 'cmdline').read_bytes() == b'catatonit\0-P\0')
    except OSError:
        return False


def label(cgroup, uid, starts_build):
    """A short fixed label for where a foreign process runs."""
    if starts_build:
        return 'starts the build (run build-image with sudo -u or runuser, not from this account\'s shell)'
    manager = f'/user.slice/user-{uid}.slice/user@{uid}.service/'
    name = cgroup.rsplit('/', 1)[-1]
    if cgroup.startswith(manager):
        if name.startswith('podman-pause-') and name.endswith('.scope'):
            return 'Podman pause scope'
        if name == 'dbus.service':
            return 'session bus'
        if name == 'init.scope':
            return 'user manager'
        return 'user manager unit'
    if cgroup.startswith(f'/user.slice/user-{uid}.slice/session-'):
        return 'login session'
    if cgroup.startswith('/system.slice/'):
        return 'system service'
    return 'other'


def foreign_processes(build, uid, ranges=(), proc=Path('/proc'), table=None):
    """(pid, label, escaped cgroup) of every process of the account outside the build's tree, sorted by PID; None if
    unobservable."""
    table = process_table(proc) if table is None else table
    if table is None or build not in table:
        return None
    build_started = table[build][4]

    def owned(uids):
        return any(value == uid or any(value in r for r in ranges) for value in uids)

    def in_build(pid):
        seen = set()
        while pid in table and pid not in seen:
            if pid == build:
                return True
            seen.add(pid)
            pid = table[pid][0]
        return False
    ancestors, current = set(), table[build][0]
    while current in table and current not in ancestors:
        ancestors.add(current)
        current = table[current][0]
    found = []
    for pid, (_, uids, cgroup, zombie, started, _threads) in sorted(table.items()):
        if zombie or not owned(uids) or in_build(pid):
            continue
        if is_pause_process(proc, pid):
            if started is not None and build_started is not None and started >= build_started:
                continue  # the build's own, new pause process
            # An earlier build's pause process held only that build's removed user namespace: killing it is safe. A
            # session account's own pause process is another matter (its Podman needs `podman system migrate` after).
            found.append((pid, 'a Podman pause process from before this build (left by an earlier build: kill '
                          + str(pid) + '; a session account then needs podman system migrate as that account)',
                          shown(cgroup)))
            continue
        found.append((pid, label(cgroup, uid, pid in ancestors), shown(cgroup)))
    return found


def watched_cgroups(name, uid):
    """The account's own cgroups: its user slice and its broker service. Nothing of the build belongs there."""
    return (f'/user.slice/user-{uid}.slice',
            f'/system.slice/system-acc\\x2dbroker.slice/acc-broker@{name}.service')


def uncounted_tasks(table, name, uid, cgroup_root=Path('/sys/fs/cgroup')):
    """(escaped cgroup, tasks) for each of the account's cgroups whose kernel task count (pids.current, which no
    forking trick escapes) exceeds the threads of every process the scan saw there; None when a count can't be read."""
    found = []
    for cgroup in watched_cgroups(name, uid):
        try:
            current = int((cgroup_root / cgroup.lstrip('/') / 'pids.current').read_text())
        except FileNotFoundError:
            continue  # no such unit or slice: nothing runs there
        except (OSError, ValueError):
            return None
        seen = sum(threads for _, _, where, _, _, threads in table.values()
                   if where == cgroup or where.startswith(cgroup + '/'))
        if current > seen:
            found.append((shown(cgroup), current - seen))
    return found


def settings_problems(name, home, *, linger=Path('/var/lib/systemd/linger'), tools=Path('/usr/bin'), run=subprocess.run):
    """Ways the account starts processes unattended. Best effort: each check that can't run is skipped."""
    problems = []
    if (linger / name).exists():
        problems.append(f'lingering is enabled (as root: loginctl disable-linger {name})')
    timers = []
    for folder in (Path(home) / '.config/systemd/user', Path(home) / '.local/share/systemd/user'):
        try:
            timers += sorted(shown(path.name, 80) for path in folder.rglob('*.timer'))
        except OSError:
            continue
    if timers:
        problems.append('user timer units exist (' + ', '.join(sorted(set(timers))[:LIMIT]) + '): disable and remove '
                        'them (systemctl --user disable --now, then delete the files)')
    for command, what, fix in (([str(tools / 'crontab'), '-l'], 'a crontab', 'crontab -r'),
                               ([str(tools / 'atq')], 'queued at jobs', 'atrm')):
        if not Path(command[0]).exists():
            continue
        try:
            result = run(command, capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            problems.append(f'{command[0]} could not be run to check for {what}')
            continue
        lines = [line for line in result.stdout.splitlines() if line.strip() and not line.lstrip().startswith('#')]
        if result.returncode == 0 and lines:
            problems.append(f'it has {what} ({fix}, after review)')
    return problems


def describe(found):
    lines = [f'  PID {pid}: {where}, cgroup {cgroup}' for pid, where, cgroup in found[:LIMIT]]
    if len(found) > LIMIT:
        lines.append(f'  and {len(found) - LIMIT} more')
    return '\n'.join(lines)


def check(step, build, *, proc=Path('/proc'), subuid=Path('/etc/subuid'), settings=False,
          cgroup_root=Path('/sys/fs/cgroup'), **options):
    """None when quiet; otherwise one refusal naming everything found."""
    entry = pwd.getpwuid(os.getuid())
    parts = []
    problems = settings_problems(entry.pw_name, entry.pw_dir, **options) if settings else []
    if problems:
        parts.append('it can start processes unattended:\n' + '\n'.join('  ' + p for p in problems))
    ranges = subordinate(entry.pw_name, entry.pw_uid, subuid)
    found, unseen = [], []
    for scan in range(SCANS):
        if scan:
            time.sleep(SCAN_GAP)
        table = process_table(proc)
        result = foreign_processes(build, entry.pw_uid, ranges, proc, table)
        if result is None:
            found = None
            break
        found += [item for item in result if item[0] not in {pid for pid, _, _ in found}]
        tasks = uncounted_tasks(table, entry.pw_name, entry.pw_uid, cgroup_root)
        if tasks is None:
            parts.append('the task count of its own cgroups could not be read, so the build can\'t be shown to be alone')
            break
        unseen = unseen or tasks
    if found:
        found.sort()
    if unseen:
        parts.append('its own cgroups hold task(s) the process list never showed (a process that keeps changing its '
                     'PID):\n' + '\n'.join(f'  {count} in {cgroup}' for cgroup, count in unseen[:LIMIT]))
    if found is None:
        parts.append('its processes could not be listed (/proc), so the build can\'t be shown to be alone')
    elif found:
        parts.append(f'it has {len(found)} other process(es):\n' + describe(found) + '\n  Stop them (its agent '
                     'sessions; its broker, socket too, or Rynhult starts it again: as root, systemctl stop '
                     f'acc-broker@{entry.pw_name}.socket acc-broker@{entry.pw_name}.service; its user manager).')
    if not parts:
        return None
    return (f'build-image: refused {step}: the building account {entry.pw_name} could change the image while it is '
            'built (security review 8, SR8-3); ' + '\n'.join(parts) + '\nThen start the build again in a new, '
            'empty folder.')


def main(argv):
    steps = {'start': 'before starting', 'save': 'before saving the image', 'checksum': 'before writing the checksum'}
    if len(argv) != 2 or argv[0] not in steps or not argv[1].isdigit() or os.getuid() == 0:
        print('usage, as the building account: build_census.py start|save|checksum BUILD_SHELL_PID', file=sys.stderr)
        return 2
    refusal = check(steps[argv[0]], int(argv[1]), settings=argv[0] == 'start')
    if refusal:
        print(refusal, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
