#!/usr/bin/python3 -I
"""Owner-only, opt-in container qualification; stdlib, no package installation."""
import argparse
import base64
import fcntl
import grp
import hashlib
import json
import os
import pwd
import re
import select
import shlex
import shutil
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2]
STATE = '/var/lib/acc/containers-qualification-kit'
STORE = '/var/lib/acc/session-images'
RUNROOT = '/run/acc-image-import'
POLICY = '/etc/acc/containers/policy.json'
RESTRICT = '/proc/sys/kernel/apparmor_restrict_unprivileged_userns'
ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'HOME': '/root', 'LANG': 'C.UTF-8'}
ACL = {'system.posix_acl_access', 'system.posix_acl_default'}


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def stamp(path, *, content=False):
    info = path.lstat()
    value = {'device': info.st_dev, 'inode': info.st_ino, 'mode': info.st_mode,
             'uid': info.st_uid, 'gid': info.st_gid,
             'xattrs': {name: base64.b64encode(os.getxattr(path, name, follow_symlinks=False)).decode()
                        for name in os.listxattr(path, follow_symlinks=False)}}
    if not stat.S_ISDIR(info.st_mode):
        value.update(size=info.st_size, mtime_ns=info.st_mtime_ns)
    if stat.S_ISLNK(info.st_mode):
        value['link'] = os.readlink(path)
    elif content and stat.S_ISREG(info.st_mode):
        value['sha256'] = sha(path)
    return value


class Host:
    # Test injection is Python-only. The CLI has no fake-root or command override.
    def __init__(self, root=Path('/'), owner=0):
        self.root, self.owner = root, owner

    def path(self, name):
        path = Path(name)
        if not path.is_absolute() or '..' in path.parts:
            raise ValueError('expected an absolute path without ..')
        return self.root / str(path).lstrip('/')

    def protected(self, path, *, directory=False):
        if not path.is_relative_to(self.root):
            raise ValueError(f'outside filesystem root: {path}')
        for item in (path, *path.parents):
            info = item.lstat()
            if (info.st_uid != self.owner or info.st_mode & 0o022 or item.is_symlink()
                    or ACL & set(os.listxattr(item))):
                raise ValueError(f'unsafe ownership, permissions, symlink or ACL: {item}')
            if item != path or directory:
                if not stat.S_ISDIR(info.st_mode):
                    raise ValueError(f'not a directory: {item}')
            elif not stat.S_ISREG(info.st_mode):
                raise ValueError(f'not a regular file: {item}')
            if item == self.root:
                break
        return path

    def run(self, args, *, timeout=120, env=None, limit=None, stderr_limit=None):
        self.protected(self.path(args[0]).resolve())
        if limit is not None:
            return bounded(args, timeout=timeout, env=env or ENV, limit=limit, stderr_limit=stderr_limit)
        # From /, never the owner console's directory (/root is 0700; an agent-identity command can't enter it).
        return subprocess.run(args, check=True, capture_output=True, text=True,
                              timeout=timeout, env=env or ENV, cwd='/').stdout.strip()


def bounded(args, *, timeout, env, limit, stderr_limit=None):
    """Stdout of a command whose output root must not trust: more than `limit` bytes or a timeout kills it. Only
    the last `stderr_limit` (default `limit`) bytes of stderr are kept, for the owner's diagnosis. Runs from /."""
    deadline = time.monotonic() + timeout
    stderr_limit = stderr_limit or limit
    out, err = b'', b''
    with subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          env=env, cwd='/') as process:
        try:
            streams = {process.stdout, process.stderr}
            while streams:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(args, timeout, output=out, stderr=err)
                for stream in select.select(list(streams), [], [], remaining)[0]:
                    chunk = os.read(stream.fileno(), 65536)
                    if not chunk:
                        streams.discard(stream)
                    elif stream is process.stdout:
                        out += chunk
                        if len(out) > limit:
                            raise ValueError(f'command output exceeds {limit} bytes')
                    else:
                        err = (err + chunk)[-stderr_limit:]
            code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except BaseException:
            process.kill()
            process.wait()
            raise
    if code:
        raise subprocess.CalledProcessError(code, args, output=out.decode(errors='replace'),
                                            stderr=err.decode(errors='replace'))
    return out.decode().strip()


PROBE_STDERR = 16384  # room for Podman's stderr and 30 debug lines of at most 300 characters

CONTROL = re.compile('[\x00-\x08\x0b-\x1f\x7f-\x9f\u061c\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]')
ANSI = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]')


def diagnostic(text, limit=4096):
    """Probe output kept for the owner: terminal escapes and control characters removed (newlines kept), the last
    `limit` characters only."""
    if isinstance(text, bytes):
        text = text.decode(errors='replace')
    return CONTROL.sub('', ANSI.sub('', text or '').replace('\r', '')).strip()[-limit:]


def subordinate_ranges(text, account, uid):
    ranges = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        parts = line.split(':')
        if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
            raise ValueError('malformed subordinate-ID entry')
        name, start, count = parts[0], int(parts[1]), int(parts[2])
        if start <= 0 or count <= 0 or start + count > 2**32 - 1:
            raise ValueError('invalid subordinate-ID range')
        ranges.append((name, start, count))
    selected = [(start, count) for name, start, count in ranges if name in (account, str(uid))]
    if not selected or max(count for start, count in selected) < 65536:
        raise ValueError(f'{account}: needs a subordinate range of at least 65536')
    for index, (_, start, count) in enumerate(ranges):
        if start <= uid < start + count:
            raise ValueError('subordinate range includes the account UID')
        for _, other, size in ranges[index + 1:]:
            if start < other + size and other < start + count:
                raise ValueError('overlapping subordinate-ID ranges')
    return selected


def atomic(path, data, mode, uid=None, gid=None, *, exclusive=False, metadata=None):
    temporary = path.parent / ('.cq-' + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            if uid is not None:
                os.fchown(stream.fileno(), uid, gid)
            if metadata:
                for name, value in metadata.get('xattrs', {}).items():
                    os.setxattr(stream.fileno(), name, base64.b64decode(value))
                if 'atime_ns' in metadata:
                    os.utime(stream.fileno(), ns=(metadata['atime_ns'], metadata['mtime_ns']))
            os.fsync(stream.fileno())
        if exclusive:
            os.link(temporary, path, follow_symlinks=False)  # atomic no-clobber publication
            temporary.unlink()
        else:
            os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if temporary.exists():
            temporary.unlink()


class Journal:
    def __init__(self, host, data):
        self.host, self.data = host, data
        self.folder = host.path(STATE)

    @classmethod
    def create(cls, host):
        host.protected(host.path(STATE).parent, directory=True)
        host.path(STATE).mkdir(mode=0o700)  # exclusive; also refuses dangling symlinks
        journal = cls(host, {'version': 1, 'status': 'installing', 'files': [], 'trees': {},
                             'settings': {}, 'pending': None})
        journal.lock = (journal.folder / '.lock').open('a')
        fcntl.flock(journal.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        journal.save()
        return journal

    @classmethod
    def load(cls, host):
        folder = host.protected(host.path(STATE), directory=True)
        path = host.protected(folder / 'manifest.json')
        data = json.loads(path.read_text())
        if data.get('version') != 1:
            raise ValueError('unsupported manifest')
        return cls(host, data)

    def save(self):
        atomic(self.folder / 'manifest.json', (json.dumps(self.data, indent=2) + '\n').encode(), 0o600)

    def finish(self, status):
        self.data['status'] = status
        self.save()

    def write(self, name, data, mode=0o644):
        path = self.host.path(name)
        self.host.protected(path.parent, directory=True)
        before = None
        if os.path.lexists(path):
            self.host.protected(path)
            original = path.stat()
            before = {'atime_ns': original.st_atime_ns, 'mtime_ns': original.st_mtime_ns,
                      'xattrs': stamp(path)['xattrs'], 'data': base64.b64encode(path.read_bytes()).decode(), 'mode': stat.S_IMODE(path.stat().st_mode),
                      'uid': path.stat().st_uid, 'gid': path.stat().st_gid}
        self.data['pending'] = name
        self.save()
        atomic(path, data, before['mode'] if before else mode,
               before['uid'] if before else None, before['gid'] if before else None,
               exclusive=before is None, metadata={'xattrs': before['xattrs']} if before else None)
        self.data['files'].append({'path': name, 'before': before, 'after': stamp(path, content=True)})
        self.data['pending'] = None
        self.save()

    def mkdir(self, name):
        path = self.host.path(name)
        self.host.protected(path.parent, directory=True)
        self.data['pending'] = name
        self.save()
        path.mkdir(mode=0o700)
        self.data['trees'][name] = {'root': stamp(path), 'entries': None}
        self.data['pending'] = None
        self.save()
        return path

    def record_tree(self, name):
        path = self.host.path(name)
        previous, current = self.data['trees'][name]['root'], stamp(path)
        if any(current[key] != previous[key] for key in ('device', 'inode')):
            raise ValueError(f'changed tree root: {name}')
        self.data['trees'][name]['root'] = current
        self.data['trees'][name]['entries'] = tree_entries(path)
        self.save()

    def validate_tree(self, name):
        recorded = self.data['trees'][name]
        path = self.host.path(name)
        if (recorded['entries'] is None or stamp(path) != recorded['root']
                or tree_entries(path) != recorded['entries']):
            raise ValueError(f'changed/unrecorded tree; preserve for owner review: {name}')

    def validate_files(self):
        if self.data['pending']:
            raise ValueError('pending operation after interruption; owner must review manifest before recovery')
        for record in self.data['files']:
            path = self.host.path(record['path'])
            if not os.path.lexists(path) or stamp(path, content=True) != record['after']:
                raise ValueError(f'changed file; refusing rollback: {path}')

    def restore_files(self):
        self.validate_files()  # all-or-refuse before changing any file
        while self.data['files']:
            record = self.data['files'][-1]
            path, before = self.host.path(record['path']), record['before']
            self.data['pending'] = record['path']
            self.save()
            if before is None:
                path.unlink()
            else:
                atomic(path, base64.b64decode(before['data']), before['mode'], before['uid'], before['gid'], metadata=before)
            self.data['files'].pop()
            self.data['pending'] = None
            self.save()


def tree_entries(path):
    result = {}
    for folder, dirs, files in os.walk(path, followlinks=False, onerror=lambda error: (_ for _ in ()).throw(error)):
        for name in dirs + files:
            child = Path(folder) / name
            if os.path.ismount(child):
                raise ValueError(f'mounted tree entry: {child}')
            result[str(child.relative_to(path))] = stamp(child)
    return result


def read_plan(path, host):
    host.protected(path)
    plan = json.loads(path.read_text())
    keys = {'account', 'archive', 'archive_sha256', 'image', 'podman_version', 'crun_version', 'repo', 'root'}
    if not isinstance(plan, dict) or set(plan) != keys or not all(isinstance(v, str) for v in plan.values()):
        raise ValueError('plan fields must exactly match plan.example.json')
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,23}-agent', plan['account']):
        raise ValueError('expected an enrolled *-agent account')
    if not re.fullmatch(r'[0-9a-f]{64}', plan['archive_sha256']) or not re.fullmatch(r'sha256:[0-9a-f]{64}', plan['image']):
        raise ValueError('replace archive/image placeholders with independently reviewed SHA-256 pins')
    for key in ('podman_version', 'crun_version'):
        if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', plan[key]):
            raise ValueError(f'{key}: exact reviewed numeric version required')
    for key in ('archive', 'repo', 'root'):
        host.path(plan[key])  # absolute, no traversal
    return plan


def review_hashes():
    paths = [SOURCE / 'scripts/qualify_containers.py']
    for name in ('scripts/containers-qualification', 'scripts/container_qualification', 'deploy/containers', 'deploy/apparmor', 'src/acc'):
        paths += [p for p in (SOURCE / name).rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc']
    return {str(p.relative_to(SOURCE)): sha(p) for p in sorted(set(paths))}


def source_protected(host):
    host.protected(SOURCE, directory=True)
    # Fresh reviewed source only: sourceless .pyc can bypass source review, and
    # empty writable package directories could acquire new modules after review.
    for name in ('src/acc', 'scripts', 'deploy/containers', 'deploy/apparmor'):
        base = SOURCE / name
        if base.exists():
            host.protected(base, directory=True)
            for path in base.rglob('*'):
                if path.name == '__pycache__' or path.suffix in ('.pyc', '.pyo'):
                    raise ValueError('unreviewed bytecode/cache in source; use a fresh committed bundle: ' + str(path))
                host.protected(path, directory=path.is_dir())
    for name in review_hashes():
        host.protected(SOURCE / name)


def containers_disabled(host):
    path = host.path('/etc/default/accd')
    if path.exists():
        host.protected(path)
        for line in path.read_text().splitlines():
            match = re.match(r'\s*(?:export\s+)?ACC_CONTAINERS\s*=\s*(.*)', line)
            if match:
                parts = shlex.split(match[1], comments=True)
                if parts and parts != ['0'] and parts != ['false'] and parts != ['no']:
                    raise ValueError('ACC_CONTAINERS must be disabled in /etc/default/accd')
    pid = int(host.run(['/usr/bin/systemctl', 'show', 'accd.service', '-p', 'MainPID', '--value']))
    if pid <= 1:
        raise ValueError('running accd MainPID unavailable; cannot prove feature is off')
    values = host.path(f'/proc/{pid}/environ').read_bytes().split(b'\0')
    flags = [value.split(b'=', 1)[1] for value in values if value.startswith(b'ACC_CONTAINERS=')]
    if any(value not in (b'', b'0', b'false', b'no') for value in flags):
        raise ValueError('running accd has ACC_CONTAINERS enabled or unknown')
    return 'configured and running ACC_CONTAINERS off (no environment secrets reported)'


def control_ready(host):
    account = pwd.getpwnam('acc')
    if account.pw_uid == 0:
        raise ValueError('acc control account must be non-root')
    path = host.path('/srv/acc/containers/grants')
    host.protected(path.parent, directory=True)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != account.pw_uid or info.st_mode & 0o027
            or ACL & set(os.listxattr(path))):
        raise ValueError('grants directory must be acc-owned and protected (0750 or stricter)')
    host.protected(host.path('/srv/acc/app/.venv/bin/python').resolve())
    return 'non-root acc account, protected driver interpreter and grants directory'


def account_groups(account, gid):
    groups = {group.gr_name for group in grp.getgrall() if account in group.gr_mem}
    groups.add(grp.getgrgid(gid).gr_name)
    administrative = {'root', 'sudo', 'admin', 'wheel', 'adm', 'docker', 'lxd', 'libvirt',
                      'disk', 'shadow', 'systemd-journal', 'acc'}
    if groups & administrative:
        raise ValueError('agent is in an administrative group: ' + ', '.join(sorted(groups & administrative)))
    return ', '.join(sorted(groups))


def preflight(host, plan, *, prepare=False):
    checks = []
    def check(name, operation, *, setup=False):
        try:
            detail = operation()
            checks.append({'name': name, 'status': 'pass', 'detail': str(detail)})
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            checks.append({'name': name, 'status': 'setup' if setup else 'missing', 'detail': str(error)})

    def expect(condition, failure, passed='verified'):
        """`failure` is the row's text only when it fails; a passing row says what was found (attempt 17's report
        showed "missing executable: ..." and "installed helper differs ..." under status pass)."""
        if not condition:
            raise ValueError(failure)
        return passed

    account = pwd.getpwnam(plan['account'])
    check('non-root enrolled account', lambda: expect(account.pw_uid > 0 and account.pw_gid > 0, 'account UID/GID must be non-root'))
    check('non-administrative agent groups', lambda: account_groups(plan['account'], account.pw_gid))
    def caller_privileges():
        values = dict(line.split(':', 1) for line in host.path('/proc/self/status').read_text().splitlines() if ':' in line)
        return expect(values.get('NoNewPrivs', '').strip() == '0', 'run from an owner console outside broker NoNewPrivileges (NoNewPrivs must be 0)')
    check('caller NoNewPrivs', caller_privileges)
    check('reviewed protected source', lambda: source_protected(host))
    for tool in ('podman', 'crun'):
        def version(tool=tool):
            output = host.run(['/usr/bin/' + tool, '--version'])
            match = re.search(r'\b\d+\.\d+\.\d+\b', output)
            observed = output.splitlines()[0] if output else 'empty output'
            return expect(match and match[0] == plan[tool + '_version'], f'{tool}: expected {plan[tool + "_version"]}; observed {observed}', observed)
        check(tool + ' version', version)
    def systemd():
        output = host.run(['/usr/bin/systemctl', '--version'])
        match = re.match(r'systemd ([0-9]+)\b', output)
        observed = output.splitlines()[0] if output else 'empty output'
        return expect(match and int(match[1]) >= 255, 'systemd >=255 required; observed ' + observed, observed)
    check('systemd version', systemd)
    check('production containers disabled', lambda: containers_disabled(host))
    check('control account and grants', lambda: control_ready(host))
    check('quiet qualification account', lambda: quiet_account(host, plan['account']))
    check('rootless Podman state', lambda: podman_state(host, plan['account']))
    for tool in ('/usr/sbin/apparmor_parser', '/usr/bin/newuidmap', '/usr/bin/newgidmap', '/usr/bin/setfacl',
                 '/usr/sbin/runuser', '/usr/bin/systemd-run', '/usr/bin/cp', '/usr/bin/python3',
                 '/usr/sbin/sysctl', '/usr/bin/env', '/usr/bin/chown', '/usr/bin/chmod',
                 '/usr/bin/find', '/usr/bin/sleep', '/usr/bin/loginctl'):
        check(tool, lambda tool=tool: expect(os.access(host.protected(host.path(tool).resolve()), os.X_OK), 'missing executable: ' + tool, 'protected executable'))
    check('AppArmor enabled', lambda: expect(host.path('/sys/module/apparmor/parameters/enabled').read_text().strip() == 'Y', 'AppArmor must be enabled'))
    check('Yama ptrace scope', lambda: expect(int(host.path('/proc/sys/kernel/yama/ptrace_scope').read_text()) >= 1, 'kernel.yama.ptrace_scope must be >=1'))
    check('userns sysctl exists', lambda: expect(host.path(RESTRICT).read_text().strip() in ('0', '1'), 'unsupported/missing restricted-userns sysctl'))
    check('restricted user namespaces', lambda: expect(host.path(RESTRICT).read_text().strip() == '1', 'kernel.apparmor_restrict_unprivileged_userns must be 1'), setup=True)
    for name in ('subuid', 'subgid'):
        check(name, lambda name=name: subordinate_ranges(host.path('/etc/' + name).read_text(), plan['account'], account.pw_uid))
    check('cgroup v2 controllers', lambda: expect({'cpu', 'memory', 'pids'} <= set(host.path('/sys/fs/cgroup/cgroup.controllers').read_text().split()), 'cgroup v2 cpu, memory and pids controllers required'))
    check('linger', lambda: expect(host.run(['/usr/bin/loginctl', 'show-user', plan['account'], '-p', 'Linger', '--value']) == 'yes', 'enable linger for ' + plan['account']), setup=True)
    def delegation():
        output = host.run(['/usr/bin/systemctl', 'show', f'user@{account.pw_uid}.service', '-p', 'Delegate', '--value'])
        expect(output == 'yes' or {'cpu', 'memory', 'pids'} <= set(output.split()), 'user manager must delegate cpu, memory, pids; observed ' + output)
        expect(host.path(f'/run/user/{account.pw_uid}/bus').is_socket(), 'user manager bus missing')
        return output
    linger_missing = next(c for c in checks if c['name'] == 'linger')['status'] != 'pass'
    check('user-manager delegation and bus', delegation, setup=linger_missing)
    for unit in ('systemd-logind.service', 'acc-containers.socket', 'acc-containers-reconcile.timer', f'acc-broker@{plan["account"]}.service'):
        check(unit, lambda unit=unit: host.run(['/usr/bin/systemctl', 'is-active', unit]))
    for installed, original in [('/usr/local/sbin/acc-containers', 'acc-containers'), ('/srv/acc/containers/acc-container-exec', 'acc-container-exec')]:
        def helper(installed=installed, original=original):
            path = host.protected(host.path(installed))
            return expect(sha(path) == sha(SOURCE / 'deploy/containers' / original), 'installed helper differs from reviewed source: ' + installed, 'matches the reviewed source')
        check(installed, helper)
    for name in ('/etc/acc/containers', '/var/lib/acc', '/run', '/etc/apparmor.d', '/etc/sysctl.d'):
        check('protected directory ' + name, lambda name=name: host.protected(host.path(name), directory=True))
    check('archive protected and checksum', lambda: expect(sha(host.protected(host.path(plan['archive']))) == plan['archive_sha256'], 'reviewed archive checksum mismatch', 'protected; checksum matches the plan'))
    check('policy', lambda: validate_policy(host, plan, allow_qualification=not prepare))
    check('AppArmor profiles', lambda: profiles_ready(host), setup=True)
    check('image execution store', lambda: store_ready(host, plan), setup=True)
    # Setup is allowed to supply only the explicitly marked reversible settings.
    ok = all(c['status'] == 'pass' or (prepare and c['status'] == 'setup') for c in checks)
    return {'version': 1, 'status': 'pass' if ok else 'blocked', 'checks': checks,
            'review_sha256': review_hashes(), 'image': plan['image'], 'archive_sha256': plan['archive_sha256']}


def validate_policy(host, plan, *, allow_qualification=False):
    value = json.loads(host.protected(host.path(POLICY)).read_text())
    if value.get('version') != 1 or not isinstance(value.get('images', {}).get('default'), dict):
        raise ValueError('existing version-1 policy with default image required')
    if 'qualification-phase1' in value['images']:
        expected = {'image': plan['image'], 'network': 'none', 'providers': {'codex': '/usr/bin/python3'}}
        if not allow_qualification or value['images']['qualification-phase1'] != expected:
            raise ValueError('qualification-phase1 policy already exists or mismatches reviewed plan')
    elif allow_qualification:
        raise ValueError('qualification-phase1 policy missing; run reviewed setup')
    return value


def profiles_ready(host):
    profiles = {line.rsplit(' (', 1)[0] for line in host.path('/sys/kernel/security/apparmor/profiles').read_text().splitlines()}
    for name in ('acc-newuidmap', 'acc-newgidmap', 'acc-pasta'):
        if name not in profiles:
            raise ValueError('missing loaded AppArmor profile: ' + name)
    if not ({'podman', 'crun'} <= profiles or {'acc-podman-cli', 'acc-crun'} <= profiles):
        raise ValueError('missing loaded Podman/crun AppArmor profiles')
    return 'mapping, pasta, podman and crun profiles loaded'


def store_ready(host, plan):
    store = host.protected(host.path(STORE), directory=True)
    root = host.protected(store / 'rootfs' / plan['image'][7:], directory=True)
    marker = host.protected(root / '.acc-image-id')
    if marker.read_text() != plan['image'] + '\n':
        raise ValueError('protected execution root marker mismatch')
    config = host.protected(host.path('/srv/acc/containers/storage.conf'))
    if config.read_bytes() != (SOURCE / 'deploy/containers/storage.conf').read_bytes():
        raise ValueError('launcher storage profile differs from reviewed source')
    for name in tree_entries(root):
        path = root / name
        info = path.lstat()
        if (info.st_uid != host.owner or (not path.is_symlink() and info.st_mode & 0o022)
                or ACL & set(os.listxattr(path, follow_symlinks=False))
                or stat.S_ISBLK(info.st_mode) or stat.S_ISCHR(info.st_mode)
                or (stat.S_ISREG(info.st_mode) and info.st_mode & (stat.S_ISUID | stat.S_ISGID))
                or 'security.capability' in os.listxattr(path, follow_symlinks=False)):
            raise ValueError('unsafe image entry: ' + str(path))
    return 'protected held-rootfs store; runtime immutability still requires collector'


def idle(host):
    active = host.run(['/usr/bin/systemctl', 'list-units', '--all', '--no-legend', '--plain',
                       '--state=active,activating,deactivating', 'acc-ct-*.service'])
    if active:
        raise ValueError('stop container sessions before setup/uninstall: ' + active)


def migrate_fix(account, uid, runtime=None):
    return (f'fix from the owner console: runuser -u {account} -- /usr/bin/env -i XDG_RUNTIME_DIR={runtime or f"/run/user/{uid}"} '
            f'PATH=/usr/bin:/bin /usr/bin/podman system migrate (never kill catatonit), then rerun preflight')


SAFE_PATH = re.compile(r'/[A-Za-z0-9._/-]{1,120}')
PAUSE_SCOPE = re.compile(r'podman-pause-[0-9a-f]{1,64}\.scope')
UNPRINTABLE = '(unprintable path)'


def read_small(path, limit=64):
    """A file the account can name or shape, read as root: never through a final link, never a FIFO or device (which
    could hang this tool, or never end like /dev/zero), never more than `limit` bytes. Returns (text, stat)."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f'{path} is not a regular file')
        data = os.read(fd, limit + 1)
    finally:
        os.close(fd)
    if len(data) > limit:
        raise ValueError(f'{path} is larger than {limit} bytes')
    return data.decode(errors='replace'), info


def pause_fix(host, account, uid, pid):
    """How to end one stray pause process. `podman system migrate` finds it only through pause.pid in its own
    XDG_RUNTIME_DIR; once the launcher's unit is gone that folder is empty or missing (qualification attempt 10), and
    the safe fix is stopping its podman-pause-*.scope as the account's user manager."""
    process = host.path(f'/proc/{pid}')
    runtime = f'/run/user/{uid}'
    try:
        for item in read_small(process / 'environ', 1 << 20)[0].split('\0'):
            if item.startswith('XDG_RUNTIME_DIR='):
                runtime = item.split('=', 1)[1]
    except (OSError, ValueError):
        pass
    shown = runtime if SAFE_PATH.fullmatch(runtime) and '..' not in runtime.split('/') else UNPRINTABLE
    try:
        reachable = (shown != UNPRINTABLE
                     and read_small(host.path(runtime + '/libpod/tmp/pause.pid'))[0].strip() == str(pid))
    except (OSError, ValueError):
        reachable = False
    if reachable:
        return migrate_fix(account, uid, runtime)
    try:
        scope = (process / 'cgroup').read_text().strip().rsplit('/', 1)[-1]
        under_manager = (process / 'cgroup').read_text().strip().startswith(
            f'0::/user.slice/user-{uid}.slice/user@{uid}.service/')
    except OSError:
        scope, under_manager = '', False
    if shown == UNPRINTABLE:  # relative, `..`, odd characters: never handed to host.path or copied
        gone = 'is not a plain absolute path'
    else:
        gone = 'no longer exists' if not os.path.lexists(host.path(runtime)) else 'no longer holds its pause.pid'
    stop = (f'/usr/bin/systemctl --user stop {scope}' if under_manager and PAUSE_SCOPE.fullmatch(scope) else
            f'/usr/bin/systemctl --user status {pid} (to find its unit), then /usr/bin/systemctl --user stop <that unit>')
    return (f'PID {pid}: its XDG_RUNTIME_DIR {shown} {gone}, so podman system migrate cannot reach it; safe fix from '
            f'the owner console, as the account\'s user manager: runuser -u {account} -- /usr/bin/env -i '
            f'XDG_RUNTIME_DIR=/run/user/{uid} PATH=/usr/bin:/bin {stop} (never kill catatonit), then rerun preflight')


def podman_state(host, account):
    """Stale rootless Podman state, read from files only (never `podman info`): a pause.pid naming a process that
    exited or isn't the account's catatonit makes Podman fail with "invalid internal status"."""
    entry = pwd.getpwnam(account)
    path = host.path(f'/run/user/{entry.pw_uid}/libpod/tmp/pause.pid')
    if not os.path.lexists(path):
        return 'no rootless Podman pause state yet'
    fix = migrate_fix(account, entry.pw_uid)
    try:
        text, info = read_small(path)
    except (OSError, ValueError):
        info = None
    if info is None or info.st_uid != entry.pw_uid:
        raise ValueError(f'stale rootless Podman state: {path} is not a small regular file owned by {account}; {fix}')
    text = text.strip()
    if not text.isdigit():
        raise ValueError(f'stale rootless Podman state: malformed {path}; {fix}')
    pid = int(text)
    try:
        comm = read_small(host.path(f'/proc/{pid}/comm'))[0].strip()
        uids = process_uids(host.path(f'/proc/{pid}'))
    except FileNotFoundError:
        raise ValueError(f'stale rootless Podman state: pause.pid names PID {pid}, which has exited (Podman reports '
                         f'"invalid internal status"); {fix}') from None
    if comm != 'catatonit' or entry.pw_uid not in uids:
        raise ValueError(f'stale rootless Podman state: pause.pid names PID {pid}, which is not {account}\'s '
                         f'catatonit; {fix}')  # comm is the account's to choose: compared, never copied
    return f'pause process {pid} (catatonit) is alive'


def process_uids(path):
    values = dict(line.split(':', 1) for line in read_small(path / 'status', 1 << 16)[0].splitlines() if ':' in line)
    return [int(value) for value in values['Uid'].split()]


SESSION_BUS = {'/usr/bin/dbus-daemon', '/usr/bin/dbus-broker-launch', '/usr/bin/dbus-broker'}


def session_bus(process, uid):
    """The account's own session bus, socket-activated in its user manager (rootless Podman's systemd cgroup manager
    starts it): an exact binary, every UID the account's, and exactly the manager's dbus.service cgroup."""
    base = f'0::/user.slice/user-{uid}.slice/user@{uid}.service/'
    if (process / 'cgroup').read_text().strip() not in (base + 'app.slice/dbus.service', base + 'session.slice/dbus.service'):
        return False
    if set(process_uids(process)) != {uid}:
        return False
    exe = os.readlink(process / 'exe')
    if exe not in SESSION_BUS:
        return False
    args = (process / 'cmdline').read_bytes().split(b'\0')
    return exe != '/usr/bin/dbus-daemon' or b'--session' in args


def account_census(host, account, entry):
    """The account's processes besides its registered broker and its user manager's init.scope, as PID lists:
    (other, pause, buses). pause holds catatonit processes, buses the session bus."""
    broker = int(host.run(['/usr/bin/systemctl', 'show', f'acc-broker@{account}.service', '-p', 'MainPID', '--value']))
    if broker <= 1:
        raise ValueError('cannot identify the registered broker MainPID')
    if entry.pw_uid not in process_uids(host.path(f'/proc/{broker}')):
        raise ValueError('broker MainPID does not belong to the selected account')
    manager = f'0::/user.slice/user-{entry.pw_uid}.slice/user@{entry.pw_uid}.service/init.scope'
    other, pause, buses = [], [], []
    for process in host.path('/proc').iterdir():
        if not process.name.isdigit() or int(process.name) == broker:
            continue
        try:
            if entry.pw_uid not in process_uids(process):
                continue
            if (process / 'cgroup').read_text().strip() == manager:
                continue
            if session_bus(process, entry.pw_uid):
                buses.append(process.name)
                continue
            if (process / 'comm').read_text().strip() == 'catatonit':
                pause.append(process.name)
            else:
                other.append(process.name)
        except FileNotFoundError:
            if process.exists():  # unreadable detail of a live process: never an allowance
                other.append(process.name)
            continue  # otherwise it exited during the census; permission errors still fail closed
    return other, pause, buses


def quiet_account(host, account, *, bus=True):
    """Only the registered broker and the account's user manager may run as the account. Before collection its
    socket-activated session bus is allowed too (`bus`); after settle_account() it must be gone as well."""
    entry = pwd.getpwnam(account)
    other, pause, buses = account_census(host, account, entry)
    if bus:
        buses = []
    problems = []
    if pause:
        problems.append('stray rootless Podman pause process (catatonit) PIDs ' + ', '.join(pause) + '; '
                        + '; '.join(dict.fromkeys(pause_fix(host, account, entry.pw_uid, pid) for pid in pause)))
    if buses:
        problems.append('the account\'s session bus is still running (PIDs ' + ', '.join(buses) + '); ' + bus_fix(account, entry.pw_uid))
    if other:
        problems.append('stop/review other account processes before proceeding (PIDs only): ' + ', '.join(other))
    if problems:
        raise ValueError('; '.join(problems))
    if not bus:
        return 'only registered broker and user-manager init.scope observed'
    return 'only registered broker, user-manager init.scope and its session bus observed'


BUS_UNITS = ('dbus.socket', 'dbus.service')


def user_systemctl(account, uid, *args):
    """systemctl --user as the account, against its own manager's private socket (no session bus needed)."""
    return ['/usr/sbin/runuser', '-u', account, '--', '/usr/bin/env', '-i', f'XDG_RUNTIME_DIR=/run/user/{uid}',
            'PATH=/usr/bin:/bin', '/usr/bin/systemctl', '--user', *args]


def bus_fix(account, uid):
    return f'fix from the owner console: {shlex.join(user_systemctl(account, uid, "stop", *BUS_UNITS))}, then rerun'


def probe_env(entry):
    """The image probe's Podman environment (image_store.podman_env, which a test keeps identical)."""
    return {'HOME': entry.pw_dir, 'XDG_RUNTIME_DIR': f'/run/user/{entry.pw_uid}', 'PATH': '/usr/bin:/bin',
            'CONTAINERS_CONF': '/dev/null', 'CONTAINERS_CONF_OVERRIDE': '/dev/null',
            'CONTAINERS_STORAGE_CONF': '/srv/acc/containers/storage.conf'}


def pause_scopes(host, uid):
    """podman-pause-*.scope units left in the account's user manager (their cgroup folders), by fixed-shape name."""
    manager = host.path(f'/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service')
    found = []
    for slice_ in ('user.slice', 'app.slice', 'session.slice'):
        try:
            names = os.listdir(manager / slice_)
        except OSError:
            continue
        found += sorted(name for name in names if PAUSE_SCOPE.fullmatch(name))
    return found


# tmux 3.4 reads these at server start (after /etc/tmux.conf, which is root's); the broker starts it without -f.
TMUX_CONFIGS = ('.tmux.conf', '.config/tmux/tmux.conf')


def broker_trust(host, account, entry):
    """What the driver's census takes on trust from the broker, as problems (census review 2026-10-25).

    It trusts the broker's MainPID, the tmux server it starts and the pane PID its launch answer names, with that
    pane's descendants. A tmux configuration in the account's home would choose the pane's shell (default-shell) and
    so run the account's code as the trusted pane; a broker started as `python -m` from its WorkingDirectory=~
    without -I imports modules from the account's home before the standard library. Only presence is checked: what
    such a file says is the account's."""
    problems = []
    found = []
    for name in TMUX_CONFIGS:
        try:
            os.lstat(host.path(entry.pw_dir) / name)
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:
            pass  # unreadable even for root: treated as present
        found.append('~/' + name)
    if found:
        problems.append(f'tmux configuration in the account\'s home ({", ".join(found)}): the broker\'s tmux server '
                        'would read it at the launch; move it out of the account\'s home, then rerun')
    unit = host.run(['/usr/bin/systemctl', 'show', f'acc-broker@{account}.service', '-p', 'ExecStart', '--value'])
    argv = re.search(r'argv\[\]=(\S+) (\S+) ', unit)
    broker = host.run(['/usr/bin/systemctl', 'show', f'acc-broker@{account}.service', '-p', 'MainPID', '--value'])
    try:
        running = read_small(host.path(f'/proc/{int(broker)}/cmdline'), 4096)[0].split('\0')
    except (OSError, ValueError):
        running = []
    if not argv or argv.group(2) != '-I' or running[1:2] != ['-I']:
        problems.append('the broker does not start Python isolated (-I), so it would import modules from the '
                        'account\'s home: install a unit whose ExecStart is `python -I -m acc.broker.server` (the '
                        'python-path-sweep change), restart acc-broker@' + account + '.service, then rerun')
    return problems


def settle_account(host, account):
    """Return the account to the quiet state preflight saw, before the launcher test (qualification attempt 13).

    The image probe runs Podman as the account with XDG_RUNTIME_DIR=/run/user/UID. Podman then leaves its rootless
    pause process (catatonit) behind, and because godbus finds the user manager's bus at /run/user/UID/bus whatever
    the cgroup manager, it moves that process into a podman-pause-*.scope there, socket-activating the session bus
    on the way. Both outlive the probe as processes of the account outside any session unit, so the driver's census
    rightly counted them as possible forgers. `podman system migrate` with the probe's own environment finds the
    pause process through that pause.pid and ends it (it also stops the account's running containers: preflight
    showed it has none); stopping dbus.socket and dbus.service ends the bus. Then the census must find nothing but
    the broker and the manager's init.scope, and no pause scope may remain.

    The probe keeps the bus reachable on purpose: hiding it changes how `podman run` places conmon and the container
    (no systemd cgroup manager), which this host-boundary probe should test as an agent would run it."""
    entry = pwd.getpwnam(account)
    env = [f'{key}={value}' for key, value in probe_env(entry).items()]
    # First a census (review L-1): migrate stops every container of the account and SIGKILLs whatever pid
    # pause.pid names, so nothing may run but what the probe is known to leave, and pause.pid must name its pause
    # process: a catatonit in a podman-pause scope of the account's user manager.
    other, pause, _ = account_census(host, account, entry)
    if other:
        raise ValueError('the account is not quiet after the image probe; nothing was stopped: stop/review other '
                         'account processes before proceeding (PIDs only): ' + ', '.join(other))
    scope_prefix = f'0::/user.slice/user-{entry.pw_uid}.slice/user@{entry.pw_uid}.service/'
    try:
        named = read_small(host.path(f'/run/user/{entry.pw_uid}/libpod/tmp/pause.pid'))[0].strip()
        cgroup = read_small(host.path(f'/proc/{named}/cgroup'), 4096)[0].strip() if named in pause else ''
    except (OSError, ValueError):
        named, cgroup = '', ''
    probes_pause = (named in pause and cgroup.startswith(scope_prefix) and '\n' not in cgroup
                    and PAUSE_SCOPE.fullmatch(cgroup.rsplit('/', 1)[-1]) is not None)
    commands = [('stop the session bus', user_systemctl(account, entry.pw_uid, 'stop', *BUS_UNITS))]
    steps = []
    if probes_pause:
        commands.insert(0, ('podman system migrate', ['/usr/sbin/runuser', '-u', account, '--', '/usr/bin/env', '-i',
                                                      *env, '/usr/bin/podman', 'system', 'migrate']))
    elif pause:
        steps.append('podman system migrate skipped: pause.pid names no pause process of the probe')
    for name, command in commands:
        # Bounded, stdin closed (review L-2): the output is the account's; the census below decides.
        try:
            host.run(command, timeout=120, limit=4096)
        except subprocess.CalledProcessError as error:
            steps.append(f'{name} exited {error.returncode}')
        except subprocess.TimeoutExpired:
            steps.append(f'{name} timed out')
        except ValueError:
            steps.append(f'{name} printed too much')
    problems = []
    try:
        quiet_account(host, account, bus=False)
    except ValueError as error:
        problems.append(str(error))
    problems += broker_trust(host, account, entry)
    scopes = pause_scopes(host, entry.pw_uid)
    if scopes:
        problems.append('Podman pause scopes remain in the account\'s user manager: ' + ', '.join(scopes) + '; fix from '
                        'the owner console: ' + shlex.join(user_systemctl(account, entry.pw_uid, 'stop', *scopes)))
    if problems:
        raise ValueError('the account is not quiet after the image probe' + (f' ({"; ".join(steps)})' if steps else '')
                         + ': ' + '; '.join(problems))
    return 'pause process and session bus ended; only registered broker and user-manager init.scope observed'


def setting(journal, name, before, after, command):
    journal.data['settings'][name] = {'before': before, 'after': after, 'pending': True}
    journal.save()
    journal.host.run(command)
    current = (journal.host.path(RESTRICT).read_text().strip() if name == 'userns' else
               journal.host.run(['/usr/bin/loginctl', 'show-user', journal.data['plan']['account'], '-p', 'Linger', '--value']))
    if current != after:
        raise ValueError('setting did not take effect: ' + name)
    journal.data['settings'][name]['pending'] = False
    journal.save()


def setup(host, plan):
    if os.path.lexists(host.path(STATE)):
        raise FileExistsError('existing kit state; use its manifest/uninstall, never rerun setup')
    for name in (STORE, RUNROOT):
        if os.path.lexists(host.path(name)):
            raise FileExistsError('refusing existing image/import store: ' + name)
    idle(host)
    result = preflight(host, plan, prepare=True)
    if result['status'] != 'pass':
        raise ValueError('preflight blocked: ' + json.dumps(result['checks']))
    for name in ('/var/lib/acc', '/run', '/srv/acc/containers', '/etc/apparmor.d', '/etc/sysctl.d'):
        host.protected(host.path(name), directory=True)
    # Readability/compatibility must be established before creating the manifest.
    loaded = {line.rsplit(' (', 1)[0] for line in host.path('/sys/kernel/security/apparmor/profiles').read_text().splitlines()}
    profile_installs = []
    for filename, names in [('acc-podman', ['acc-newuidmap', 'acc-newgidmap', 'acc-pasta'])]:
        path = host.path('/etc/apparmor.d/' + filename)
        present = [name in loaded for name in names]
        if os.path.lexists(path):
            host.protected(path)
            if path.read_bytes() != (SOURCE / 'deploy/apparmor' / filename).read_bytes() or not all(present):
                raise ValueError('existing AppArmor profile differs or is not loaded: ' + filename)
        elif any(present):
            raise ValueError('loaded AppArmor profile without its reviewed file: ' + filename)
        else:
            profile_installs.append(filename)
    distro = [host.path('/etc/apparmor.d/' + name).exists() for name in ('podman', 'crun')]
    runtime = host.path('/etc/apparmor.d/acc-podman-runtime')
    if all(distro):
        for name in ('podman', 'crun'):
            host.protected(host.path('/etc/apparmor.d/' + name))
        if runtime.exists() or not all(name in loaded for name in ('podman', 'crun')):
            raise ValueError('conflicting/unloaded distribution Podman/crun profiles')
    elif any(distro):
        raise ValueError('only one distribution runtime profile exists; resolve manually, never double-attach')
    elif runtime.exists():
        host.protected(runtime)
        if runtime.read_bytes() != (SOURCE / 'deploy/apparmor/acc-podman-runtime').read_bytes():
            raise ValueError('existing runtime profile differs')
        profiles_ready(host)
    elif any(name in loaded for name in ('podman', 'crun', 'acc-podman-cli', 'acc-crun')):
        raise ValueError('runtime profile loaded without reviewed file')
    else:
        profile_installs.append('acc-podman-runtime')
    policy = validate_policy(host, plan)
    journal = Journal.create(host)
    journal.data.update(plan=plan, source=str(SOURCE), review_sha256=review_hashes(), profiles=[])
    journal.save()
    try:
        linger_marker = host.path('/var/lib/systemd/linger/' + plan['account'])
        if os.path.lexists(linger_marker):
            host.protected(linger_marker)
        else:
            setting(journal, 'linger', 'no', 'yes', ['/usr/bin/loginctl', 'enable-linger', plan['account']])
        # This starts only the requested user manager, not ACC or other host services.
        account = pwd.getpwnam(plan['account'])
        # enable-linger starts the manager asynchronously; start waits for it (owner run 2026-10-25 raced is-active).
        host.run(['/usr/bin/systemctl', 'start', f'user@{account.pw_uid}.service'])
        host.run(['/usr/bin/systemctl', 'is-active', f'user@{account.pw_uid}.service'])
        for filename in profile_installs:
            target = '/etc/apparmor.d/' + filename
            journal.write(target, (SOURCE / 'deploy/apparmor' / filename).read_bytes())
            journal.data['profiles'].append({'path': target, 'loaded': False, 'pending': True})
            journal.save()
            host.run(['/usr/sbin/apparmor_parser', '--skip-cache', '-a', host.path(target).as_posix()])
            journal.data['profiles'][-1].update(loaded=True, pending=False)
            journal.save()
        before = host.path(RESTRICT).read_text().strip()
        if before != '1':
            setting(journal, 'userns', before, '1', ['/usr/sbin/sysctl', '-w', 'kernel.apparmor_restrict_unprivileged_userns=1'])
        journal.write('/etc/sysctl.d/90-acc-containers-qualification.conf',
                      b'# Owner container qualification; restore with the kit uninstall.\nkernel.apparmor_restrict_unprivileged_userns=1\n')
        publish_image(journal, plan)
        journal.write('/srv/acc/containers/storage.conf', (SOURCE / 'deploy/containers/storage.conf').read_bytes())
        policy['images']['default']['image'] = plan['image']
        policy['images']['qualification-phase1'] = {'image': plan['image'], 'network': 'none',
                                                    'providers': {'codex': '/usr/bin/python3'}}
        journal.write(POLICY, (json.dumps(policy, indent=2) + '\n').encode())
        qualification = host.path('/etc/acc/containers/qualification')
        if not os.path.lexists(qualification):
            journal.mkdir('/etc/acc/containers/qualification')
            qualification.chmod(0o755)
            journal.record_tree('/etc/acc/containers/qualification')
        else:
            host.protected(qualification, directory=True)
        journal.finish('ready')
        return journal
    except BaseException:
        journal.finish('failed')
        raise


def publish_image(journal, plan):
    host = journal.host
    journal.mkdir(STORE)
    journal.mkdir(RUNROOT)
    copied = journal.folder / 'reviewed.oci.tar'
    config = journal.folder / 'import-storage.conf'
    env = {**ENV, 'CONTAINERS_CONF': '/dev/null', 'CONTAINERS_CONF_OVERRIDE': '/dev/null',
           'CONTAINERS_STORAGE_CONF': str(config)}
    prefix = ['/usr/bin/podman', '--root', str(host.path(STORE)), '--runroot', str(host.path(RUNROOT)), '--storage-driver', 'overlay']
    mounted = False
    try:
        # Source is protected; exclusive destination and independent copy, never a hardlink.
        with host.path(plan['archive']).open('rb') as source, copied.open('xb') as target:
            shutil.copyfileobj(source, target)
            os.fchmod(target.fileno(), 0o600)
        config.write_text('[storage]\ndriver = "overlay"\n')
        host.run(['/usr/bin/python3', '-I', '-B', str(SOURCE / 'deploy/containers/verify_image_archive.py'),
                  str(copied), plan['archive_sha256'], plan['image']], timeout=1800)
        host.run([*prefix, 'load', '--input', str(copied)], timeout=1800, env=env)
        actual = host.run([*prefix, 'image', 'inspect', '--format', '{{.Id}}', plan['image']], env=env)
        if actual.removeprefix('sha256:') != plan['image'][7:]:
            raise ValueError('imported image ID mismatch')
        mount = host.run([*prefix, 'image', 'mount', plan['image']], env=env)
        mounted = True
        mount_path = Path(mount)
        if not mount_path.is_absolute() or not mount_path.is_relative_to(host.path(STORE)):
            raise ValueError('image mount outside newly owned store')
        rootfs = host.path(STORE) / 'rootfs'
        rootfs.mkdir(mode=0o700)
        pending = rootfs / '.pending'
        pending.mkdir(mode=0o700)
        host.run(['/usr/bin/cp', '-a', '--reflink=never', mount + '/.', str(pending) + '/'], timeout=1800)
        mounted = False  # a failed unmount is unknown state; do not retry it
        host.run([*prefix, 'image', 'unmount', plan['image']], env=env)
        fd = os.open(pending / '.acc-image-id', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o444)
        with os.fdopen(fd, 'w') as stream:
            stream.write(plan['image'] + '\n')
        pending.rename(rootfs / plan['image'][7:])
        harden(host.path(STORE))
        host.run(['/usr/bin/setfacl', '-Rb', str(host.path(STORE))], timeout=1800)
        host.run(['/usr/bin/find', str(host.path(STORE)), '-type', 'd', '-exec',
                  '/usr/bin/setfacl', '-k', '{}', '+'], timeout=1800)
        host.run(['/usr/bin/chown', '-hR', 'root:root', str(host.path(STORE))], timeout=1800)
        host.run(['/usr/bin/chmod', '-R', 'a+rX,go-w', str(host.path(STORE))], timeout=1800)
    finally:
        if mounted:
            # Unmount only the exact image in the freshly created store. Failure
            # leaves a mounted tree, which the uninstall will explicitly refuse.
            host.run([*prefix, 'image', 'unmount', plan['image']], env=env)
        for name in (STORE, RUNROOT):
            journal.record_tree(name)


def harden(store):
    """Before the new store is opened to other accounts: no device node (an overlay whiteout is c 0 0, not a
    device), and no setuid/setgid bit or file capability, from the agent-built image. Stripped deliberately,
    not left to chown's side effect. The store stays 0700 if this refuses."""
    for folder, dirs, files in os.walk(store, followlinks=False, onerror=lambda error: (_ for _ in ()).throw(error)):
        for name in dirs + files:
            path = os.path.join(folder, name)
            info = os.lstat(path)
            if stat.S_ISBLK(info.st_mode) or (stat.S_ISCHR(info.st_mode) and info.st_rdev != 0):
                raise ValueError('device node in imported image; refusing to publish: ' + path)
            if not stat.S_ISREG(info.st_mode):
                continue
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise ValueError('image entry changed type during publication: ' + path)
                if os.fstat(fd).st_mode & (stat.S_ISUID | stat.S_ISGID):
                    os.fchmod(fd, stat.S_IMODE(os.fstat(fd).st_mode) & ~(stat.S_ISUID | stat.S_ISGID))
                if 'security.capability' in os.listxattr(fd):
                    os.removexattr(fd, 'security.capability')
            finally:
                os.close(fd)


def validate_settings(journal):
    host, plan = journal.host, journal.data['plan']
    for name, record in journal.data['settings'].items():
        if record['pending']:
            raise ValueError('pending setting; owner review required: ' + name)
        current = (host.path(RESTRICT).read_text().strip() if name == 'userns' else
                   host.run(['/usr/bin/loginctl', 'show-user', plan['account'], '-p', 'Linger', '--value']))
        if current != record['after']:
            raise ValueError('setting changed outside kit: ' + name)


def uninstall(journal):
    host = journal.host
    if journal.data['status'] == 'uninstalled':
        raise ValueError('already uninstalled; manifest retained as evidence')
    source_protected(host)  # rollback runs as root too; setup and collect check this through preflight
    idle(host)
    quiet_account(host, journal.data['plan']['account'])
    if any(p.get('pending') for p in journal.data.get('profiles', [])):
        raise ValueError('pending AppArmor load; owner must review kernel profile state')
    journal.validate_files()
    validate_settings(journal)
    for name in journal.data['trees']:
        journal.validate_tree(name)
    if journal.data.get('probe_unit'):
        raise ValueError('collector probe cleanup incomplete; review exact recorded unit before uninstall')
    # Prevalidate everything before the first rollback operation. No generic rm -rf.
    for profile in reversed(journal.data.get('profiles', [])):
        if profile['loaded']:
            host.run(['/usr/sbin/apparmor_parser', '--skip-cache', '-R', str(host.path(profile['path']))])
            profile['loaded'] = False
            journal.save()
    removed_files = {str(host.path(r['path'])) for r in journal.data['files'] if r['before'] is None}
    journal.restore_files()
    for name in list(reversed(journal.data['settings'])):
        record = journal.data['settings'][name]
        if name == 'userns':
            host.run(['/usr/sbin/sysctl', '-w', 'kernel.apparmor_restrict_unprivileged_userns=' + record['before']])
        elif name == 'linger':
            host.run(['/usr/bin/loginctl', 'disable-linger', journal.data['plan']['account']])
        del journal.data['settings'][name]
        journal.save()
    for name in list(reversed(journal.data['trees'])):
        root = host.path(name)
        # The inventory contains every object we created. Refuse unknown additions.
        for relative in sorted(journal.data['trees'][name]['entries'], key=lambda p: (p.count('/'), p), reverse=True):
            path = root / relative
            if str(path) in removed_files:
                continue  # already removed by the validated file rollback above
            if path.is_dir() and not path.is_symlink():
                path.rmdir()
            else:
                path.unlink()
        root.rmdir()
        del journal.data['trees'][name]
        journal.save()
    journal.finish('uninstalled')


def write_new(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())


BOOTSTRAP = '''import os, runpy, sys
ns = os.stat('/proc/self/ns/user')
guard = runpy.run_path('/srv/acc/containers/acc-container-exec')
guard['enter_namespace'](int(sys.argv[1]), int(sys.argv[2]), (ns.st_dev, ns.st_ino))
os.execl('/usr/bin/sleep', 'sleep', '900')
'''


def collect(journal):
    host, plan = journal.host, journal.data['plan']
    if journal.data['status'] != 'ready' or journal.data.get('collection_started'):
        raise ValueError('collection requires ready setup and runs once; preserve existing evidence')
    journal.validate_files()
    for name in journal.data['trees']:
        journal.validate_tree(name)
    validate_settings(journal)
    # Refuse an existing proof. Do not replace evidence produced by another run.
    proof_path = f'/etc/acc/containers/qualification/{plan["account"]}.json'
    if os.path.lexists(host.path(proof_path)):
        raise FileExistsError('existing root proof: ' + proof_path)
    report_path = journal.folder / 'report.json'
    if os.path.lexists(report_path):
        raise FileExistsError('existing qualification report')
    journal.data['collection_started'] = True
    journal.save()
    report = {'version': 1, 'status': 'no-go', 'go_phase_1': False, 'image': plan['image'],
              'archive_sha256': plan['archive_sha256'], 'started_at': time.time(), 'production_qualified': False,
              'review_sha256': journal.data['review_sha256'], 'checks': {}}
    exit_code = 1
    try:
        check = preflight(host, plan)
        report['preflight'] = check
        if check['status'] != 'pass':
            raise ValueError('ready preflight failed; see each missing prerequisite')
        account = pwd.getpwnam(plan['account'])
        # Check freshness of sources against setup; do not execute a later edit.
        if review_hashes() != journal.data['review_sha256']:
            raise ValueError('reviewed source changed since setup')
        unit = 'acc-cq-' + uuid.uuid4().hex
        journal.data['probe_unit'] = unit  # journal intent before dispatch; timeout may still create it
        journal.save()
        host.run(['/usr/bin/systemd-run', '--quiet', '--collect', '--unit=' + unit,
                  '--uid=0', '--gid=0', '--working-directory=/', '--expand-environment=no',
                  '--property=PrivateUsers=no', '--property=PrivateMounts=yes', '--property=Type=exec',
                  '/usr/bin/python3', '-I', '-B', '-c', BOOTSTRAP, str(account.pw_uid), str(account.pw_gid)])
        try:
            pid = int(host.run(['/usr/bin/systemctl', 'show', '-p', 'MainPID', '--value', unit]))
            if pid <= 1:
                raise ValueError('probe unit has no valid MainPID')
            for attempt in range(100):
                if host.path(f'/proc/{pid}/comm').read_text().strip() == 'sleep':
                    break
                time.sleep(0.1)
            else:
                raise ValueError('namespace bootstrap did not become ready')
            if host.path(f'/proc/{pid}').stat().st_uid != account.pw_uid:
                raise ValueError('namespace probe did not drop to the selected agent')
            if not host.path(f'/proc/{pid}').exists():
                raise ValueError(f'namespace service PID {pid} exited before the probe ran; see unit {unit}')
            try:
                proof = json.loads(host.run(['/usr/sbin/runuser', '-u', plan['account'], '--', '/usr/bin/env', '-i',
                           'HOME=' + account.pw_dir, 'PATH=/usr/bin:/bin', 'XDG_RUNTIME_DIR=/run/user/' + str(account.pw_uid),
                           '/usr/bin/python3', '-I', '-B', str(SOURCE / 'scripts/container_qualification/image_store.py'),
                           plan['image'], str(pid)], timeout=300, limit=4096, stderr_limit=PROBE_STDERR))
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
                # Keep what the probe said: its last stderr line names the failed check.
                stderr = diagnostic(error.stderr, PROBE_STDERR)
                failed = {'status': 'fail', 'exit_code': getattr(error, 'returncode', None),
                          'timed_out': isinstance(error, subprocess.TimeoutExpired),
                          'stdout': diagnostic(error.output), 'stderr': stderr,
                          # Podman's own stderr from the failing call, and its --log-level=debug rerun.
                          'podman': [line[8:] for line in stderr.splitlines() if line.startswith('podman: ')],
                          'podman_debug': [line[14:] for line in stderr.splitlines() if line.startswith('podman-debug: ')]}
                report['checks']['image_store'] = failed
                reason = (failed['stderr'].splitlines() or ['no output'])[-1]
                raise ValueError(f'root-captured image probe failed (exit {failed["exit_code"]}): {reason}') from None
            # The probe runs as the agent: write only what root validated, stamped with root's capture time.
            expected = {'account': plan['account'], 'image': plan['image'], 'status': 'pass',
                        'direct_write': 'denied', 'unshare_write': 'denied', 'runtime_layers': 'protected',
                        'execution_source': 'held-rootfs', 'namespace_boundary': 'denied'}
            if (not isinstance(proof, dict) or any(proof.get(key) != value for key, value in expected.items())
                    or type(proof.get('files')) is not int or not 0 < proof['files'] <= 10_000_000):
                raise ValueError('root-captured image probe did not pass')
            proof = {**expected, 'files': proof['files'], 'time': time.time()}
            journal.write(proof_path, (json.dumps(proof, indent=2) + '\n').encode())
            # This directory may itself have been created by setup.
            directory = '/etc/acc/containers/qualification'
            if directory in journal.data['trees']:
                journal.record_tree(directory)
            report['checks']['image_store'] = proof
        finally:
            host.run(['/usr/bin/systemctl', 'stop', unit])
            journal.data.pop('probe_unit')
            journal.save()
        # The probe ran Podman as the account; its pause process and session bus must not reach the launcher test.
        report['settle'] = settle_account(host, plan['account'])
        # Root orchestrates; only the existing unprivileged acc driver calls the broker.
        phase_plan = {key: plan[key] for key in ('account', 'image', 'repo', 'root')}
        phase_plan['policy_id'] = 'qualification-phase1'
        driver_plan = journal.folder / 'phase1-plan.json'
        write_new(driver_plan, phase_plan)
        # acc can read only this public plan, not the root-only journal or evidence.
        public_plan = '/etc/acc/containers/qualification/kit-plan.json'
        if os.path.lexists(host.path(public_plan)):
            raise FileExistsError('existing collector plan: ' + public_plan)
        journal.write(public_plan, (json.dumps(phase_plan) + '\n').encode())
        if '/etc/acc/containers/qualification' in journal.data['trees']:
            journal.record_tree('/etc/acc/containers/qualification')
        command = ['/usr/sbin/runuser', '-u', 'acc', '--', '/usr/bin/env', '-i', 'HOME=' + pwd.getpwnam('acc').pw_dir,
                   'PATH=/usr/bin:/bin', 'PYTHONPATH=' + str(SOURCE / 'src'),
                   'PYTHONPYCACHEPREFIX=' + str(journal.folder / 'unused-bytecode'),
                   '/srv/acc/app/.venv/bin/python', '-B',
                   str(SOURCE / 'scripts/qualify_containers.py'), '--enable-prototype', '--phase-1-plan', public_plan]
        try:
            payload = host.run(command, timeout=1800)
            driver_exit = 0
        except subprocess.CalledProcessError as error:
            if error.returncode != 1:
                raise
            payload, driver_exit = error.stdout, 1
        phase1 = json.loads(payload)
        report['phase1'] = phase1
        mandatory = ('trusted_launcher', 'claude_hooks', 'codex_hooks', 'copilot_hooks', 'codex_hook_trust',
                     'codex_bundled_bwrap', 'chrome_sandbox', 'network_none', 'per_session_limits',
                     'restart_stop_cleanup', 'immutable_inputs', 'container_boundary', 'image_store')
        if (driver_exit != 0 or phase1.get('go_phase_1') is not True
                or set(phase1.get('checks', {})) != set(mandatory)
                or any(phase1['checks'][name].get('status') != 'pass' for name in mandatory)):
            raise ValueError('trusted launcher qualification returned NO-GO; see phase1 rows')
        # A scoped phase-1 GO is never a production qualification claim.
        report.update(status='scoped-phase1-pass', go_phase_1=True,
                      production_qualified=False, limitations='See docs/CONTAINERS.md security-review and remaining qualification gates')
        exit_code = 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        report['error'] = str(error)
    except Exception as error:  # anything unexpected is still a recorded NO-GO, never a pass
        report['error'] = f'unexpected {type(error).__name__}: {error}'
    finally:
        report['finished_at'] = time.time()
        write_new(report_path, report)
        print(f'Report: {report_path}\nSHA256 {sha(report_path)}')
    return exit_code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['preflight', 'setup', 'collect', 'uninstall'])
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--prepare', action='store_true', help='preflight before reversible setup; show setup-owned gaps')
    args = parser.parse_args()
    host = Host()
    try:
        if (args.prepare and args.command != 'preflight') or (args.plan and args.command in ('collect', 'uninstall')):
            raise ValueError('--prepare belongs to preflight; --plan belongs to preflight/setup')
        if args.command != 'preflight' and os.geteuid() != 0:
            raise ValueError('owner root console required; nothing changed')
        if args.command in ('preflight', 'setup'):
            if not args.plan:
                raise ValueError('--plan is required')
            plan = read_plan(args.plan, host)
            if args.command == 'preflight':
                report = preflight(host, plan, prepare=args.prepare)
                print(json.dumps(report, indent=2))
                return 0 if report['status'] == 'pass' else 1
            journal = setup(host, plan)
            print(f'Setup ready. Manifest: {journal.folder / "manifest.json"}')
            print(json.dumps(journal.data['review_sha256'], indent=2))
            return 0
        journal = Journal.load(host)
        with (journal.folder / '.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Reload under the lock, so stale collector/uninstall state cannot win.
            journal = Journal.load(host)
            if args.command == 'collect':
                return collect(journal)
            uninstall(journal)
            print('Restored recorded settings/files; evidence and manifest retained. Containers remain off.')
            return 0
    except Exception as error:  # every failure, expected or not, is exit 2 (unsafe state), never 1 or a traceback
        print('REFUSED: ' + str(error), file=sys.stderr)
        print('Stop. Preserve the manifest/evidence; do not rerun setup or delete unknown state.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
