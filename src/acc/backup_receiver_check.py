"""Standalone, isolated receiver disk guard; copied into the owner's setup script."""
from __future__ import annotations

import json
import os
import pwd
import stat
import subprocess
import sys
from pathlib import Path

DATA = Path('/var/lib/acc-backup')
BINDING = Path('/etc/acc/backup-target.json')
DISK = ('Backup receiver disk check failed: require acc-backup ownership, mode 0700 and the recorded device/fsid. '
        'Mount dedicated volumes BEFORE setup. Mount a volume elsewhere and bind-mount its acc-backup subfolder; '
        'never use the filesystem root (including lost+found).')
# At receiver start the binding exists, so re-recording after a reviewed volume change is the way on. During setup
# (--preflight, --record) nothing is bound yet: the setup block's own trap says to correct the mount and rerun it.
ADVICE = (DISK + ' After reviewing the mount, record it as root: '
          '/usr/bin/python3 -I /usr/local/libexec/acc-backup-receiver-check --rerecord')
NOT_BOUND = ('No receiver binding yet (/etc/acc/backup-target.json): run the target setup commands from Rynhult '
             'first. --rerecord is only for a receiver that is already set up.')


def filesystem_root(path, *, mountinfo=Path('/proc/self/mountinfo')):
    # Unlike ismount(), distinguish a whole-filesystem mount from a bind-mounted subdirectory.
    def unescape(value):
        for code, char in [('040', ' '), ('011', '\t'), ('012', '\n'), ('134', '\\')]:
            value = value.replace('\\' + code, char)
        return value
    resolved = str(Path(path).resolve())
    if resolved == '/':
        return True
    lines = mountinfo.read_text().splitlines()
    matches = [line.split() for line in lines if len(line.split()) >= 6 and unescape(line.split()[4]) == resolved]
    return bool(matches and unescape(matches[-1][3]) == '/')


def identity(path, uid, *, inspect_entries=False):
    path = Path(path)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o700
            or filesystem_root(path)):
        raise ValueError(ADVICE)
    if inspect_entries:
        def fail(error):
            raise ValueError(ADVICE) from None
        for directory, folders, files in os.walk(path, followlinks=False, onerror=fail):
            for entry in [Path(directory), *(Path(directory) / name for name in folders + files)]:
                entry_info = entry.lstat()
                access = os.R_OK | (os.X_OK if stat.S_ISDIR(entry_info.st_mode) else 0)
                if stat.S_ISLNK(entry_info.st_mode) or not os.access(entry, access, effective_ids=True):
                    raise ValueError(ADVICE)
    return {'device': info.st_dev, 'fsid': os.statvfs(path).f_fsid}


def verify(path, binding, uid, *, inspect_entries=False):
    actual = identity(path, uid, inspect_entries=inspect_entries)
    if not isinstance(binding, dict) or any(type(binding.get(key)) is not int or binding[key] != value for key, value in actual.items()):
        raise ValueError(ADVICE)
    return actual


def rerecord(*, runner=subprocess.run):
    """Root, after the owner reviewed the mount: scan as acc-backup again and replace only the binding's identity."""
    if os.geteuid() != 0:
        raise ValueError('Run --rerecord as root.')
    if DATA.is_symlink() or filesystem_root(DATA):
        raise ValueError(DISK + ' Correct the mount, then run --rerecord again.')
    try:
        fd = os.open(BINDING, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        raise ValueError(NOT_BOUND) from None
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or info.st_size > 4096:
            raise ValueError('Unsafe receiver binding: review /etc/acc/backup-target.json before re-recording')
        binding = json.load(stream)
    if not isinstance(binding, dict):
        raise ValueError('Unsafe receiver binding: review /etc/acc/backup-target.json before re-recording')
    scan = runner(['runuser', '-u', 'acc-backup', '--', sys.executable, '-I', os.path.abspath(__file__), '--record'],
                  capture_output=True, text=True, timeout=3600)
    if scan.returncode:
        raise ValueError((scan.stderr.strip()[:1000] or DISK) + ' Correct the mount, then run --rerecord again.')
    recorded = json.loads(scan.stdout)
    if sorted(recorded) != ['device', 'fsid'] or any(type(value) is not int for value in recorded.values()):
        raise ValueError(ADVICE)
    previous = {key: binding.get(key) for key in recorded}
    staging = BINDING.with_name('.' + BINDING.name + '.new')
    fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, 'w') as stream:
        os.fchmod(stream.fileno(), 0o644)
        stream.write(json.dumps({**binding, **recorded}) + '\n')
    os.replace(staging, BINDING)
    print(f'Recorded receiver disk {recorded} (was {previous}). Start it: systemctl restart acc-backup-receiver.service')


def main():
    setup = sys.argv[1:] in (['--preflight'], ['--record'])
    try:
        if sys.argv[1:] == ['--rerecord']:
            rerecord()
            return 0
        if sys.argv[1:] == ['--preflight']:
            if os.geteuid() != 0:
                raise ValueError('Run the setup block as root; nothing was changed.')
            if DATA.is_symlink() or filesystem_root(DATA):
                raise ValueError(DISK)
            return 0
        uid = pwd.getpwnam('acc-backup').pw_uid
        if os.geteuid() != uid:
            raise ValueError('Receiver guard must run as acc-backup')
        if sys.argv[1:] == ['--record']:
            try:
                print(json.dumps(identity(DATA, uid, inspect_entries=True)))
            except ValueError:
                raise ValueError(DISK) from None
        elif not sys.argv[1:]:
            verify(DATA, json.loads(BINDING.read_text()), uid, inspect_entries=True)
        else:
            raise ValueError('Unsupported receiver guard arguments')
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        # Only our exact ValueErrors are safe messages; JSONDecodeError can contain parser details.
        print(str(error) if type(error) is ValueError else DISK if setup else ADVICE, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
