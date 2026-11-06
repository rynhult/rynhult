#!/usr/bin/python3 -I
"""Provision the fixed Linux policy helper during owner-invoked install/update.

Policy publication is enabled; automatic OS grants remain separately qualified.
Never executes repository code as part of a folder request.
"""
import argparse
import json
import os
import pwd
import re
import shlex
import stat
import sys
from pathlib import Path, PurePosixPath

# Keep aligned with acc.folder_policy.SYSTEM; installer runs without the app environment.
SYSTEM = ('/etc', '/root', '/proc', '/sys', '/dev', '/run', '/boot', '/usr', '/bin', '/sbin',
          '/lib', '/lib32', '/lib64', '/tmp', '/var/tmp')

MODULES = ('__init__.py', 'folder_worker.py', 'folder_policy.py', 'folder_acl.py', 'folder_host.py')


def directory(path, mode=0o755):
    path = Path(path)
    if path != path.parent:
        directory(path.parent)
    if not path.exists() and not path.is_symlink():
        path.mkdir(mode=mode)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError('Unsafe folder helper directory.')
    return path


def read_private(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or info.st_mode & 0o022:
            raise ValueError('Unsafe folder helper configuration.')
        raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError('Oversized folder helper configuration.')
        return raw


def settings(path):
    values = {}
    for line in read_private(path).splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, sep, value = line.partition('=')
        if not sep:
            raise ValueError('Invalid installed configuration.')
        parts = shlex.split(value, comments=True)
        values[key.strip()] = ' '.join(parts)
    # New public spelling wins, matching acc.env.Environment.
    values.update({'ACC_' + k[8:]: v for k, v in list(values.items()) if k.startswith('RYNHULT_')})
    return values


def publish(path, data, mode):
    # Replace links rather than opening their targets; parent is root-protected.
    import tempfile
    fd, name = tempfile.mkstemp(prefix='.stage-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        parent = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


protected_lstat = os.lstat


def validate_protected(value, setting):
    """Keep lexical policy comparisons identical to filesystem destinations."""
    if (not value.startswith('/') or value == '/'
            or any(part in ('', '.', '..') for part in value.split('/')[1:])):
        raise ValueError(f'{setting}: protected path must be absolute with no empty, . or .. components: {value!r}')
    current = Path('/')
    for part in value.split('/')[1:]:
        current = current / part
        try:
            info = protected_lstat(current)
        except FileNotFoundError:
            # Descendants cannot exist once a parent is missing.
            break
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f'{setting}: protected path contains a symlink: {value!r}')
    return value


def validate_home(value, protected, *, owner=False):
    home = PurePosixPath(value)
    if (not home.is_absolute() or len(home.parts) < 3
            or any(part in ('', '.', '..') for part in value.split('/')[1:])
            or any(c in value for c in ':$~\\') or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError('Unsafe owner or agent home.')
    for value in (*SYSTEM, *protected):
        other = PurePosixPath(value)
        # Only the human owner's home can safely contain protected paths.
        # An agent could replace a descendant with a link before an owner backup.
        if home == other or other in home.parents or (not owner and home in other.parents):
            raise ValueError('Owner or agent home overlaps a protected or system folder.')


def merge_protected(existing, generated):
    # Refresh every administrator-associated account, even if its broker is gone.
    # Never add an association or change reviewed roots/qualification choices.
    discovered = set(generated['protected'])
    existing['protected'] = sorted(set(existing.get('protected', ())) | discovered)
    for value in existing['protected']:
        validate_protected(value, 'worker.json protected')
    for account in existing['accounts'].values():
        protected = sorted(set(account.get('protected', ())) | discovered)
        for value in protected:
            validate_protected(value, 'worker.json account protected')
        # Installer associations store the human owner first, then the agent.
        for index, home in enumerate(account.get('homes', ())):
            validate_home(home, protected, owner=index == 0)
        account['protected'] = protected
    return existing


def mapping(config, brokers, owner_name, service_name, *, lookup=pwd.getpwnam):
    owner, service = lookup(owner_name), lookup(service_name)
    if owner.pw_uid < 1000 or service.pw_uid == 0:
        raise ValueError('Expected separate human and service identities.')
    protected = {'/srv/acc', '/etc/acc', '/var/lib/acc-folder-policy', '/var/backups/acc', service.pw_dir}
    for value in protected:
        validate_protected(value, 'service home / built-in protected paths')
    # Discover custom service paths, including secret files and external storage.
    for key, value in config.items():
        if key.startswith('ACC_') and key not in ('ACC_AGENT_HOME', 'ACC_BACKUP_MIRRORS'):
            # Settings such as ACC_ALLOWED_ORIGINS hold URLs: 'https://host'.split(':') yields '//host', not a path.
            protected.update(validate_protected(p, key) for p in value.split(':') if p.startswith('/') and not p.startswith('//'))
        if key == 'ACC_BACKUP_MIRRORS':
            for entry in value.split(';'):
                if entry:
                    parts = entry.split(':')
                    if len(parts) != 3 or not all(p.startswith('/') for p in parts[1:]):
                        raise ValueError('ACC_BACKUP_MIRRORS: invalid backup mirror mapping.')
                    protected.update(validate_protected(p, key) for p in parts[1:])
    validate_home(owner.pw_dir, protected, owner=True)
    accounts = {}
    for name, env in brokers.items():
        if env.get('ACC_BROKER_ROLE') != 'agents':
            continue
        try:
            agent = lookup(name)
        except KeyError:
            print(f'Warning: skipping broker configuration for deleted user {name!r}.', file=sys.stderr)
            continue
        if (not re.fullmatch('[a-z_][a-z0-9_-]{0,31}', name) or name in ('root', 'acc', 'acc-node')
                or agent.pw_uid < 1000 or agent.pw_uid in (owner.pw_uid, service.pw_uid, 65534)):
            raise ValueError('Expected a separate assigned agent identity.')
        validate_home(agent.pw_dir, protected)
        roots = env['ACC_ALLOWED_ROOTS'].split(':')
        if not roots or any(not p.startswith('/') or '\n' in p for p in roots):
            raise ValueError('Invalid configured broker roots.')
        accounts[name] = {'uid': agent.pw_uid, 'owner_uid': owner.pw_uid,
            'homes': [owner.pw_dir, agent.pw_dir], 'protected': sorted(protected), 'roots': roots,
            'broker_socket': '/run/acc-broker/' + name + '.sock',
            'broker_unit': f'acc-broker@{name}.service', 'broker_socket_unit': f'acc-broker@{name}.socket'}
    return {'machine_id': 'local', 'service_uid': service.pw_uid, 'policy_qualified': True,
            'qualified': False, 'shared_group_qualified': False, 'protected': sorted(protected), 'accounts': accounts}


def configuration(owner=None, agent=None, *, root=Path('/')):
    """Read and validate the installed mapping without creating or publishing anything."""
    path = root / 'etc/acc/folders/worker.json'
    config = settings(root / 'etc/default/accd')
    backup = root / 'etc/default/acc-backup'
    if backup.exists() or backup.is_symlink():
        config.update(settings(backup))
    broker_dir = root / 'etc/acc/broker'
    try:
        broker_paths = list(broker_dir.iterdir())
    except FileNotFoundError:
        broker_paths = []
    brokers = {p.stem: settings(p) for p in broker_paths if p.match('*.env')}
    owner = owner or config.get('ACC_SERVICES_ACCOUNT')
    if not owner:
        raise ValueError('Set ACC_SERVICES_ACCOUNT in /etc/default/accd.')
    agent = agent or Path(config.get('ACC_AGENT_HOME') or f'/home/{owner}-agent').name
    generated = mapping(config, brokers, owner, 'acc')
    if path.exists() or path.is_symlink():
        existing = json.loads(read_private(path))
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise ValueError('The folder mapping must be private root configuration.')
        # Preserve administrator mappings and qualification choices on upgrade.
        # New accounts require an explicit administrator association, never guess
        # another person's home from a multi-user account name.
        if existing['machine_id'] != 'local' or existing['service_uid'] != generated['service_uid']:
            raise ValueError('Installed folder helper identity differs; administrator review required.')
        existing = merge_protected(existing, generated)
        return existing
    else:
        # Only the installer-selected agent has an established human association.
        selected = agent
        generated['accounts'] = {k: v for k, v in generated['accounts'].items() if k == selected}
        if not generated['accounts']:
            raise ValueError('No installer-selected agent mapping.')
        return generated


def dry_run(owner=None, agent=None, *, root=Path('/')):
    """Validate available installed settings, without requiring an installed host or root."""
    primary = root / 'etc/default/accd'
    try:
        primary.lstat()
    except FileNotFoundError:
        print('folder mapping: nothing installed yet, skipped')
        return
    except PermissionError as exc:
        print(f'folder mapping: skipped: needs root to read {exc.filename}')
        return
    try:
        configuration(owner, agent, root=root)
    except PermissionError as exc:
        print(f'folder mapping: skipped: needs root to read {exc.filename}')
        return
    print('Folder mapping validation passed (dry run: nothing is changed).')


def install(repo, owner, agent):
    if os.geteuid() != 0:
        raise ValueError('Run through the owner-invoked root installer.')
    generated = configuration(owner, agent)
    base = directory('/usr/local/lib/acc-folders')
    package = directory(base / 'acc')
    for name in MODULES:
        publish(package / name, (repo / 'src/acc' / name).read_bytes(), 0o644)
    publish(base / 'worker.py', (repo / 'deploy/folders/worker.py').read_bytes(), 0o644)
    config_dir = directory('/etc/acc/folders', 0o700)
    state = directory('/var/lib/acc-folder-policy', 0o700)
    if any(stat.S_IMODE(p.stat().st_mode) != 0o700 for p in (config_dir, state)):
        raise ValueError('Folder configuration and state must be private.')
    publish(config_dir / 'worker.json', json.dumps(generated, sort_keys=True).encode(), 0o600)
    units = directory('/etc/systemd/system')
    for name in ('acc-folder-policy.socket', 'acc-folder-policy@.service'):
        publish(units / name, (repo / 'deploy/folders' / name).read_bytes(), 0o644)
    # No service start here: install.sh reloads and enables the fixed socket.


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--owner')
    parser.add_argument('--agent')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--root', type=Path, default=Path('/'), help='Offline root; dry-run only')
    args = parser.parse_args()
    if args.root != Path('/') and not args.dry_run:
        parser.error('--root requires --dry-run')
    if args.dry_run:
        dry_run(args.owner, args.agent, root=args.root)
    else:
        install(args.repo, args.owner, args.agent)
