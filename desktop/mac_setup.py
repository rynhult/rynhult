"""One-time macOS Installer account provisioning. JSON dry-run is portable.

No elevation API or resident root helper: --apply runs only from protected
installed resources as root on Darwin. Never use a writable checkout with root.
"""
from __future__ import annotations

import argparse
import grp
import json
import os
import plistlib
import pwd
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

PREFIX = Path('/Library/Application Support/Rynhult Desktop')
RESOURCES = PREFIX / 'Runtime'
# macOS 13+ sshd_config reads these drop-ins first; Remote Login starts sshd per connection, so no reload.
SSHD_CONFIG = '/etc/ssh/sshd_config'
SSHD_DROPINS = '/etc/ssh/sshd_config.d'
# staff owns every home (0750); admin can authorize as root. dsmemberutil also resolves nested/calculated groups.
FORBIDDEN_GROUPS = {20: 'staff', 80: 'admin'}
READ_RIGHTS = {'read', 'read_data', 'list', 'search', 'execute', 'readattr', 'readextattr', 'readsecurity',
               'file_inherit', 'directory_inherit', 'limit_inherit', 'only_inherit', 'inherited'}


def locations(uid: int) -> dict:
    agent = f'rynd-{uid}'
    base = PREFIX / 'Owners' / str(uid)
    config = PREFIX / 'Config' / str(uid)
    return dict(agent=agent, base=str(base), home=str(base / agent), run_dir=str(base / 'run'),
                projects=str(base / 'projects'), socket=str(PREFIX / 'Sockets' / str(uid) / f'{agent}.sock'),
                manifest=str(config / 'execution.json'), identity=str(config / 'identity.json'),
                plist=f'/Library/LaunchDaemons/com.rynhult.desktop.{uid}.plist',
                label=f'com.rynhult.desktop.{uid}')


def check_acl_output(text: str) -> None:
    for line in text.splitlines():
        if re.match(r'^\s*\d+:', line) and ' allow ' in line:
            rights = set(line.split(' allow ', 1)[1].strip().split(','))
            if rights - READ_RIGHTS:
                raise ValueError('Protected resource has a writable macOS ACL')


def protected(paths) -> None:
    """POSIX metadata plus Darwin ACLs; a mode-only check misses write grants."""
    paths = list(dict.fromkeys(Path(p) for p in paths))
    for path in paths:
        info = path.lstat()
        if info.st_uid != 0 or info.st_mode & 0o022 or stat.S_ISLNK(info.st_mode):
            raise ValueError(f'Expected root-owned protected path: {path}')
    if sys.platform == 'darwin':
        for start in range(0, len(paths), 128):
            result = subprocess.run(['/bin/ls', '-lde', *map(str, paths[start:start + 128])],
                                    check=True, capture_output=True, text=True,
                                    env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
            check_acl_output(result.stdout)


def protected_resources() -> None:
    # Symlinked runtime libraries may point inside the same protected package.
    links, ordinary = [], [RESOURCES, *RESOURCES.parents]
    for path in RESOURCES.rglob('*'):
        if path.is_symlink():
            if path.lstat().st_uid != 0 or not path.resolve().is_relative_to(RESOURCES):
                raise ValueError('Package link escapes protected resources')
            links.append(path.resolve())
        else:
            ordinary.append(path)
    protected([*ordinary, *links])


def check_sshd_includes(text: str) -> None:
    if not re.search(r'^\s*Include\s+' + re.escape(SSHD_DROPINS) + r'/\*\s*$', text, re.MULTILINE | re.IGNORECASE):
        raise ValueError(f'Remote Login would not read the agent denial: {SSHD_CONFIG} lacks Include {SSHD_DROPINS}/* (macOS 13+)')


def check_groups(agent_uid: int) -> None:
    for gid, name in FORBIDDEN_GROUPS.items():
        result = subprocess.run(['/usr/bin/dsmemberutil', 'checkmembership', '-u', str(agent_uid), '-g', str(gid)],
                                capture_output=True, text=True, env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
        answer = result.stdout.strip()
        if answer == 'user is a member of the group':
            raise ValueError(f'Dedicated agent must never belong to {name}')
        if answer != 'user is not a member of the group':
            raise ValueError(f'Could not confirm the agent is outside {name}; group membership unknown')


def plan(action: str, owner, agent_id: int, *, purge=False, identity=None) -> dict:
    uid = owner.pw_uid
    if uid < 501 or uid == 65534 or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]{0,63}', owner.pw_name):
        raise ValueError('Owner must be an ordinary macOS login')
    if not 50000 <= agent_id < 60000 or agent_id in (uid, owner.pw_gid):
        raise ValueError('Use an unused dedicated UID/GID from 50000 through 59999')
    if action not in {'install', 'uninstall'} or (purge and action != 'uninstall'):
        raise ValueError('Invalid setup action')
    paths = locations(uid)
    agent = paths['agent']
    base, home, run, projects = (Path(paths[k]) for k in ('base', 'home', 'run_dir', 'projects'))
    value = dict(version=1, platform='darwin', owner_uid=uid, owner_gid=owner.pw_gid,
                 agent_uid=agent_id, agent_gid=agent_id, resources=str(RESOURCES), **paths,
                 agent_guid=str(uuid.uuid4()).upper(), group_guid=str(uuid.uuid4()).upper())
    if identity is not None:
        validate_manifest(identity, uid)
        if identity['agent_uid'] != agent_id or identity['owner_gid'] != owner.pw_gid:
            raise ValueError('Installed identity differs from the selected owner/account')
        value = identity
    daemon = dict(Label=paths['label'], UserName=agent, GroupName=agent,
        ProgramArguments=[str(RESOURCES / 'desktop/runtime/python/bin/python3'), '-I',
                          str(RESOURCES / 'desktop/mac_broker.py'), str(uid)],
        WorkingDirectory=str(home), Umask=0o027, AbandonProcessGroup=True,
        ProcessType='Standard', ThrottleInterval=10, KeepAlive={'SuccessfulExit': False},
        EnvironmentVariables={'HOME': str(home), 'PATH': f'{home}/.local/bin:{RESOURCES}/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin',
                              'LANG': 'en_US.UTF-8', 'SHELL': '/bin/sh', 'PYTHONNOUSERSITE': '1', 'PYTHONDONTWRITEBYTECODE': '1'},
        Sockets={'Broker': dict(SockPathName=paths['socket'], SockPathOwner=uid, SockPathGroup=owner.pw_gid,
                                SockPathMode=0o600, SockType='stream', SockPassive=True)})
    prepare = [['/usr/bin/install', '-d', '-o', 'root', '-g', 'wheel', '-m', '0755',
        str(PREFIX), str(PREFIX / 'Owners'), str(PREFIX / 'Config'), str(PREFIX / 'Sockets'),
        str(Path(paths['manifest']).parent), str(Path(paths['socket']).parent)]]
    commands = []
    group = f'/Groups/{agent}'
    person = f'/Users/{agent}'
    for record, attributes in ((group, {'GeneratedUID': value['group_guid'], 'PrimaryGroupID': str(agent_id), 'Password': '*'}),
            (person, {'GeneratedUID': value['agent_guid'], 'UniqueID': str(agent_id), 'PrimaryGroupID': str(agent_id),
                      'NFSHomeDirectory': str(home), 'UserShell': '/usr/bin/false', 'Password': '*',
                      'IsHidden': '1', 'RealName': f'Rynhult desktop agent for uid {uid}'})):
        for name, content in attributes.items():
            commands.append(['/usr/bin/dscl', '.', '-create', record, name, content])
    commands += [['/usr/bin/install', '-d', '-o', 'root', '-g', 'wheel', '-m', '0711', str(base)],
                 ['/usr/bin/install', '-d', '-o', 'root', '-g', 'wheel', '-m', '0711', str(run)],
                 ['/usr/bin/install', '-d', '-o', agent, '-g', agent, '-m', '0700', str(home),
                  str(home / '.local'), str(home / '.local/bin'), str(run / agent), str(projects)]]
    # POSIX 0700 does not remove inherited ACL grants. All these paths are new;
    # strip inherited access before adding only the intended owner project/spool ACLs.
    for folder in (base, home, home / '.local', home / '.local/bin', run, run / agent, projects):
        commands.append(['/bin/chmod', '-N', str(folder)])
    # Owners need project files and read-only spools, never the credential home.
    for folder, rights in ((projects, 'list,search,add_file,add_subdirectory,delete_child,read,write,append,readattr,writeattr,readextattr,writeextattr,readsecurity'),
                           (run, 'list,search,read,readattr,readextattr,readsecurity'),
                           (run / agent, 'list,search,read,readattr,readextattr,readsecurity')):
        commands.append(['/bin/chmod', '+a', f'user:{owner.pw_name} allow {rights},file_inherit,directory_inherit', str(folder)])
    # Password '*' and a false shell still allow OpenSSH public keys and -N forwarding for a key the agent plants.
    ssh_denial = f'{SSHD_DROPINS}/100-rynhult-desktop-{uid}.conf'
    files = {paths['manifest']: json.dumps(value, indent=2) + '\n',
             ssh_denial: f'# Rynhult desktop: the hidden agent for uid {uid} never signs in over Remote Login.\nDenyUsers {agent}\n',
             paths['plist']: plistlib.dumps(daemon, sort_keys=True).decode()}
    remove, stop, cleanup = [], None, []
    finish = [['/bin/launchctl', 'bootstrap', 'system', paths['plist']]]
    if action == 'uninstall':
        prepare = []
        commands = [['/bin/launchctl', 'bootout', f"system/{paths['label']}"]]
        files, finish = {}, []
        remove = [paths['plist'], paths['manifest'], paths['socket']]
        # AbandonProcessGroup keeps tmux and anything the agent started alive after bootout: end all of it,
        # including jobs launchd or cron would start again. Best effort; the exit wait below is the check.
        stop = ['tmux', '-L', f'rynhult-desktop-{uid}', 'kill-server']
        cleanup = [['/bin/launchctl', 'bootout', f'user/{agent_id}'], ['/usr/bin/crontab', '-r', '-u', agent],
                   ['/usr/bin/pkill', '-KILL', '-U', str(agent_id)]]
        if purge:
            commands += [['/usr/bin/dscl', '.', '-delete', person], ['/usr/bin/dscl', '.', '-delete', group]]
            remove += [ssh_denial, paths['identity']]
    return dict(apply=False, action=action, owner=owner.pw_name, owner_uid=uid, agent=agent,
                agent_id=agent_id, purge=purge, identity=value, prepare=prepare, commands=commands,
                files=files, finish=finish, remove=remove, stop_tmux=stop, cleanup=cleanup)


def validate_manifest(value: dict, uid: int) -> None:
    expected = locations(uid)
    if (value.get('version') != 1 or value.get('platform') != 'darwin' or value.get('owner_uid') != uid
            or any(value.get(k) != v for k, v in expected.items())
            or value.get('resources') != str(RESOURCES)
            or not 50000 <= value.get('agent_uid', 0) < 60000
            or value.get('agent_gid') != value.get('agent_uid') or value['agent_uid'] == uid):
        raise ValueError('macOS execution identity or fixed paths differ from setup')
    for key in ('agent_guid', 'group_guid'):
        uuid.UUID(value[key])


def read_identity(uid: int, *, active=True) -> dict | None:
    path = Path(locations(uid)['manifest' if active else 'identity'])
    if not path.exists():
        return None
    protected([path, *path.parents])
    value = json.loads(path.read_text())
    validate_manifest(value, uid)
    return value


def record(path: str) -> dict:
    result = subprocess.run(['/usr/bin/dscl', '-plist', '.', '-read', path],
                            capture_output=True, check=True)
    return plistlib.loads(result.stdout)


def verify_identity(value: dict, groups=True) -> None:
    """The retained GUID receipt prevents deleting/adopting a replacement account."""
    for kind, guid, idkey in (('Users', 'agent_guid', 'UniqueID'), ('Groups', 'group_guid', 'PrimaryGroupID')):
        data = record(f"/{kind}/{value['agent']}")
        if data.get('dsAttrTypeStandard:GeneratedUID') != [value[guid]]:
            raise ValueError('Installed account/group GeneratedUID changed; refusing deletion')
        numbers = data.get('dsAttrTypeStandard:' + idkey)
        if numbers is not None and numbers != [str(value['agent_uid'])]:
            raise ValueError('Installed account/group numeric identity changed')
    if data.get('dsAttrTypeStandard:GroupMembership', []):
        raise ValueError('Private agent group unexpectedly has explicit members')
    user_record = record(f"/Users/{value['agent']}")
    if user_record.get('dsAttrTypeNative:IsHidden') != ['1']:
        raise ValueError('Dedicated agent account must stay hidden')
    account = pwd.getpwnam(value['agent'])
    if account.pw_uid != value['agent_uid'] or account.pw_gid != value['agent_gid'] or account.pw_dir != value['home'] or account.pw_shell != '/usr/bin/false':
        raise ValueError('Dedicated account identity changed')
    if grp.getgrnam('admin').gr_gid in os.getgrouplist(account.pw_name, account.pw_gid):
        raise ValueError('Dedicated agent must never belong to admin')
    if groups:  # removal must stay possible for an account install refused for its groups
        check_groups(value['agent_uid'])


def write_exclusive(name: str, content: str) -> None:
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, 'w') as stream:
        stream.write(content)


def wait_for_agent_exit(uid: int, timeout=5) -> None:
    deadline = time.monotonic() + timeout
    while True:
        result = subprocess.run(['/bin/ps', '-axo', 'uid=,pid='], check=True, capture_output=True, text=True,
                                env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
        remaining = [int(row[1]) for line in result.stdout.splitlines()
                     if (row := line.split()) and row[0] == str(uid)]
        if not remaining:
            return
        if time.monotonic() >= deadline:
            raise ValueError(f'Agent processes remain: {remaining}; stop only their recorded PIDs and retry purge')
        time.sleep(.1)


def apply_plan(value: dict) -> None:
    if sys.platform != 'darwin':
        raise ValueError('--apply is supported only on macOS; use the portable dry run here')
    if os.geteuid() != 0:
        raise ValueError('--apply requires macOS Installer administrator authorization')
    if Path(__file__).resolve().parents[1] != RESOURCES:
        raise ValueError('Apply only the installed root-owned Runtime copy, never the source checkout')
    protected_resources()
    info = value['identity']
    validate_manifest(info, value['owner_uid'])
    actual_owner = pwd.getpwnam(value['owner'])
    if (actual_owner.pw_uid, actual_owner.pw_gid) != (info['owner_uid'], info['owner_gid']):
        raise ValueError('Desktop owner identity changed')
    existing = [p for p in [PREFIX, *PREFIX.parents, PREFIX / 'Owners', PREFIX / 'Config', PREFIX / 'Sockets',
                           Path(info['manifest']).parent, Path(info['socket']).parent,
                           Path('/Library/LaunchDaemons'), Path(SSHD_DROPINS)] if p.exists()]
    protected(existing)
    clean = {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LC_ALL': 'C'}

    def run(argv):
        subprocess.run(argv, check=True, env=clean, stdin=subprocess.DEVNULL)

    if value['action'] == 'install':
        search = '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin'
        if any(shutil.which(name, path=search) is None for name in ('tmux', 'git', 'node', 'npm')):
            raise ValueError('Install accessible tmux, Git, Node and npm first; setup never runs brew')
        for lookup, name in ((pwd.getpwnam, info['agent']), (grp.getgrnam, info['agent']),
                             (pwd.getpwuid, info['agent_uid']), (grp.getgrgid, info['agent_gid'])):
            try:
                lookup(name)
            except KeyError:
                continue
            raise ValueError('Account/group or UID/GID already exists; refusing adoption')
        if any(os.path.lexists(info[key]) for key in ('base', 'manifest', 'identity', 'plist', 'socket')) or any(
                os.path.lexists(name) for name in value['files']):
            raise ValueError('Desktop state already exists; refusing overwrite')
        if not Path(SSHD_DROPINS).is_dir():
            raise ValueError(f'Remote Login drop-in folder {SSHD_DROPINS} is missing (macOS 13+ required)')
        check_sshd_includes(Path(SSHD_CONFIG).read_text())
        for required in ('desktop/runtime/python/bin/python3', 'desktop/mac_broker.py', 'bin/acc-hook', 'bin/acc-guard'):
            if not (RESOURCES / required).is_file():
                raise ValueError('Installed macOS desktop resources are incomplete')
        for argv in value['prepare']:
            run(argv)
        # Receipt survives a later install failure, for administrator-reviewed recovery.
        write_exclusive(info['identity'], json.dumps(info, indent=2) + '\n')
        for argv in value['commands']:
            run(argv)
        verify_identity(info)
        for name, content in value['files'].items():
            write_exclusive(name, content)
        for argv in value['finish']:
            run(argv)
        return
    stored = read_identity(info['owner_uid'], active=False)
    if stored != info:
        raise ValueError('Uninstall needs the exact protected installation receipt')
    verify_identity(info, groups=False)
    bootout = value['commands'][0]
    result = subprocess.run(bootout, env=clean, capture_output=True, text=True)
    if result.returncode and 'No such process' not in result.stderr and 'Could not find service' not in result.stderr:
        raise ValueError('launchd refused unloading; no files or accounts removed: ' + result.stderr.strip())
    # Execute the known tmux server command after dropping privileges, never as root.
    env = {'HOME': info['home'], 'PATH': f"{info['home']}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
           'LANG': 'en_US.UTF-8', 'SHELL': '/bin/sh'}
    stopped = subprocess.run(value['stop_tmux'], env=env, user=info['agent_uid'], group=info['agent_gid'],
                             extra_groups=[], cwd=info['home'], capture_output=True, text=True, timeout=15)
    if stopped.returncode not in (0, 1):
        raise ValueError('Could not stop the exact desktop tmux server')
    for argv in value['cleanup']:  # nothing running, no user domain or no crontab is fine
        subprocess.run(argv, env=clean, stdin=subprocess.DEVNULL, capture_output=True)
    wait_for_agent_exit(info['agent_uid'])
    if value['purge']:
        if any(p.pw_gid == info['agent_gid'] and p.pw_uid != info['agent_uid'] for p in pwd.getpwall()):
            raise ValueError('Another account now uses the agent group; refusing purge')
        if not shutil.rmtree.avoids_symlink_attacks:
            raise ValueError('This Python cannot remove data safely; manual administrator recovery required')
        base = Path(info['base'])
        protected([base])
        shutil.rmtree(base)
        for argv in value['commands'][1:]:
            run(argv)
    for name in value['remove']:
        Path(name).unlink(missing_ok=True)
    if value['purge']:
        Path(info['manifest']).parent.rmdir()
        Path(info['socket']).parent.rmdir()
        if value.get('remove_runtime'):
            if any((PREFIX / 'Config').iterdir()):
                raise ValueError('Other owner receipts remain; runtime retained')
            shutil.rmtree(RESOURCES)
            for directory in ('Owners', 'Config', 'Sockets'):
                try:
                    (PREFIX / directory).rmdir()
                except OSError:
                    pass
            try:
                PREFIX.rmdir()
            except OSError:
                pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('install', 'uninstall'))
    parser.add_argument('--owner', required=True)
    parser.add_argument('--owner-uid', type=int, help='portable plan input; apply verifies against Directory Services')
    parser.add_argument('--owner-gid', type=int)
    parser.add_argument('--agent-id', type=int)
    parser.add_argument('--purge-agent', action='store_true', help='uninstall and permanently delete agent/data')
    parser.add_argument('--confirm-agent', help='required exact account name for destructive apply')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--remove-runtime', action='store_true', help='purge only: remove shared runtime if no other owner receipts remain')
    args = parser.parse_args()
    try:
        if (args.owner_uid is None) != (args.owner_gid is None):
            raise ValueError('Provide both owner UID and GID for a portable plan')
        owner = (pwd.getpwnam(args.owner) if args.owner_uid is None else
                 SimpleNamespace(pw_name=args.owner, pw_uid=args.owner_uid, pw_gid=args.owner_gid))
        identity = read_identity(owner.pw_uid, active=False) if sys.platform == 'darwin' and args.action == 'uninstall' else None
        if identity and args.agent_id is not None and args.agent_id != identity['agent_uid']:
            raise ValueError('Requested agent UID/GID differs from the protected receipt')
        agent_id = identity['agent_uid'] if identity else (args.agent_id if args.agent_id is not None else 50000)
        value = plan(args.action, owner, agent_id, purge=args.purge_agent, identity=identity)
        if args.remove_runtime and not (args.action == 'uninstall' and args.purge_agent):
            raise ValueError('Removing runtime requires explicit agent purge')
        value['remove_runtime'] = args.remove_runtime
        if args.apply:
            if args.purge_agent and args.confirm_agent != value['agent']:
                raise ValueError('Destructive purge requires --confirm-agent with the exact agent name')
            apply_plan(value)
            value['apply'] = True
        print(json.dumps(value, indent=2))
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, f'macOS setup refused: {error}\n')


if __name__ == '__main__':
    main()
