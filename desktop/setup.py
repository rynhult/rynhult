"""Desktop-only, administrator-reviewed Linux provisioning. Default is dry-run.

Run the installed, root-owned packaged copy with its bundled Python. No shell
commands, user-selected install paths, sudo policy or recurring root helper.
"""
from __future__ import annotations

import argparse
import json
import os
import pwd
import shutil
import stat
import subprocess
import time
from pathlib import Path

RESOURCES = Path(__file__).resolve().parents[1]
# An expired account refuses every PAM login (SSH public keys despite the locked password, su, cron, at), so
# its code only runs inside the broker unit and its NoNewPrivileges. systemd's User= does not consult PAM.
EXPIRED = '1970-01-02'


def trusted_tree(path: Path) -> None:
    for item in (path, *path.parents, *path.rglob('*')):
        info = item.lstat()
        if info.st_uid != 0 or (not stat.S_ISLNK(info.st_mode) and info.st_mode & 0o022):
            raise ValueError(f'Execution resources must be root-owned and not writable: {item}')
        if item.is_symlink() and not item.resolve().is_relative_to(path):
            raise ValueError(f'Execution resource link escapes package: {item}')


def validate_destinations(value: dict) -> None:
    # O_EXCL/O_NOFOLLOW protect only the final file. Every existing ancestor must
    # also be an administrator-controlled directory before commands or unlink.
    parents = [Path(name).parent for name in (*value['files'], *value['remove'])]
    parents += [Path(value['base']).parent, Path(value['runtime_dir'])]
    for path in parents:
        for item in (path, *path.parents):
            try:
                info = item.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise ValueError(f'Unsafe desktop destination directory: {item}')


def plan(action: str, owner) -> dict:
    uid = owner.pw_uid
    agent = f'rynd-{uid}'
    base = Path(f'/var/lib/rynhult-desktop/{uid}')
    conf = Path(f'/etc/rynhult-desktop/{uid}')
    home, run, projects = base / agent, base / 'run', base / 'projects'
    unit = f'rynhult-desktop-{uid}'
    socket = f'/run/rynhult-desktop/{uid}/{agent}.sock'
    manifest = dict(version=1, owner_uid=uid, agent=agent, home=str(home), run_dir=str(run),
                    projects=str(projects), socket=socket, resources=str(RESOURCES))
    unit_base = Path('/etc/systemd/system') / unit
    files = {
        str(conf / 'execution.json'): json.dumps(manifest, indent=2) + '\n',
        str(unit_base) + '.socket': f'''[Unit]
Description=Rynhult desktop broker for uid {uid}
[Socket]
ListenStream={socket}
SocketUser={uid}
SocketMode=0600
DirectoryMode=0755
[Install]
WantedBy=sockets.target
''',
        str(unit_base) + '.service': f'''[Unit]
Description=Rynhult desktop agent for uid {uid}
Requires={unit}.socket
After={unit}.socket
[Service]
Type=simple
User={agent}
Group={agent}
WorkingDirectory={home}
Environment=HOME={home}
Environment=PATH={home}/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=PYTHONNOUSERSITE=1
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart="{RESOURCES}/desktop/runtime/python/bin/python3" -I "{RESOURCES}/desktop/broker.py" {uid}
UMask=0027
NoNewPrivileges=yes
ProtectHome=yes
KillMode=process
Restart=on-failure
''',
    }
    commands = [
        ['install', '-d', '-m', '0755', '/var/lib/rynhult-desktop', '/etc/rynhult-desktop'],
        ['install', '-d', '-m', '0711', str(base)],
        ['useradd', '--create-home', '--home-dir', str(home), '--shell', '/bin/bash', '--user-group',
         '--expiredate', EXPIRED, agent],
        ['passwd', '-l', agent],
        ['chmod', '0700', str(home)],
        # The run root stays root's, as on a server: the agent owns only its own spool below it.
        ['install', '-d', '-m', '0711', str(run)],
        ['install', '-d', '-o', agent, '-g', agent, '-m', '0700',
         str(run / agent), str(projects), str(home / '.local'), str(home / '.local/bin')],
        ['setfacl', '-m', f'u:{uid}:rwx,d:u:{uid}:rwx,d:u::rwx,d:g::---,d:o::---', str(run / agent), str(projects)],
        # The no-follow ingest walker opens this trusted root O_RDONLY|O_DIRECTORY.
        # Keep it root-owned/non-writable; grant only this owner the read/traverse needed for that open.
        ['setfacl', '-m', f'u:{uid}:r-x', str(run)],
        ['install', '-d', '-m', '0755', str(conf)],
    ]
    remove, cleanup = [], []
    if action == 'uninstall':
        # KillMode=process keeps tmux and agent work alive when the broker stops: end all of it and block the
        # account from running anything again. Account, credentials and data stay for recovery.
        commands = [['usermod', '--lock', '--expiredate', EXPIRED, agent]]
        cleanup = [['systemctl', 'disable', '--now', unit + '.socket'],
                   ['systemctl', 'stop', unit + '.service'],
                   ['crontab', '-r', '-u', agent],
                   ['pkill', '-KILL', '-u', agent]]
        # systemd sockets default to RemoveOnStop=no; remove only this stopped owner's endpoint.
        # unlink never follows the final component, including an unexpected symlink.
        remove = [*files, socket]
        files = {}
    return dict(apply=False, action=action, owner=owner.pw_name, agent=agent, base=str(base),
                runtime_dir=str(Path(socket).parent), owner_uid=uid,
                manifest=str(conf / 'execution.json'), commands=commands,
                cleanup=cleanup, files=files, remove=remove,
                finish=[['systemctl', 'daemon-reload']] +
                ([['systemctl', 'enable', '--now', unit + '.socket']] if action == 'install' else []))


def read_provisioning(path: str) -> dict:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise ValueError('Untrusted provisioning record')
            raw = stream.read(65537)
            if len(raw) > 65536:
                raise ValueError('Oversized provisioning record')
            return json.loads(raw)
    except (OSError, ValueError) as error:
        raise ValueError('A protected desktop provisioning record is required; review recovery before uninstall') from error


def validate_uninstall(value: dict) -> int:
    saved = read_provisioning(value['manifest'])
    base = Path(value['base'])
    expected = dict(version=1, owner_uid=value['owner_uid'], agent=value['agent'],
                    home=str(base / value['agent']), run_dir=str(base / 'run'),
                    projects=str(base / 'projects'), resources=str(RESOURCES),
                    socket=str(Path(value['runtime_dir']) / (value['agent'] + '.sock')))
    if not isinstance(saved, dict) or any(saved.get(key) != val for key, val in expected.items()):
        raise ValueError('Desktop provisioning identity differs; refusing to alter the account')
    try:
        account = pwd.getpwnam(value['agent'])
    except KeyError as error:
        raise ValueError('The provisioned desktop account is missing; review recovery') from error
    if (account.pw_uid < 1000 or account.pw_uid in (value['owner_uid'], 65534)
            or account.pw_dir != expected['home']):
        raise ValueError('Desktop provisioning account differs; refusing to alter it')
    return account.pw_uid


def active_uid(uid: int, proc: Path = Path('/proc')) -> bool:
    for process in proc.iterdir():
        if not process.name.isdigit():
            continue
        try:
            fields = dict(line.split(':', 1) for line in (process / 'status').read_text().splitlines() if ':' in line)
        except FileNotFoundError:
            continue  # process exited during the scan
        except OSError as error:
            raise ValueError('Desktop cleanup cannot verify process ownership') from error
        identities = fields.get('Uid', '').split()
        state = fields.get('State', '').split()
        if len(identities) != 4 or not state:
            raise ValueError('Desktop cleanup cannot verify process status')
        # Zombies/dead tasks cannot execute; do not wait for an unrelated parent to reap them.
        if str(uid) in identities and state[0] not in ('Z', 'X'):
            return True
    return False


def wait_for_uid_exit(uid: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while active_uid(uid):
        if time.monotonic() >= deadline:
            raise ValueError('Desktop cleanup could not confirm agent exit; provisioning retained')
        time.sleep(0.1)


def apply_plan(value: dict) -> None:
    if os.geteuid() != 0:
        raise ValueError('--apply requires an administrator; dry-run needs no privileges')
    trusted_tree(RESOURCES)
    validate_destinations(value)
    if value['action'] == 'uninstall':
        agent_uid = validate_uninstall(value)
    if value['action'] == 'install':
        missing = [name for name in ('tmux', 'setfacl', 'git', 'node', 'npm', 'python3', 'pkill', 'crontab') if shutil.which(name) is None]
        if missing:
            raise ValueError('Install execution prerequisites first: ' + ', '.join(missing))
        try:
            pwd.getpwnam(value['agent'])
        except KeyError:
            pass
        else:
            raise ValueError('Agent account already exists; refusing to adopt it')
        if any(Path(name).exists() for name in value['files']):
            raise ValueError('Desktop configuration already exists; refusing to overwrite it')
        if os.path.lexists(value['base']):
            raise ValueError(f"Agent data folder {value['base']} already exists; refusing to adopt it")
        for required in ('desktop/runtime/python/bin/python3', 'desktop/broker.py', 'bin/acc-hook', 'bin/acc-guard'):
            if not (RESOURCES / required).is_file():
                raise ValueError(f'Incomplete installed desktop package: {required}')
    for command in value['commands']:
        subprocess.run(command, check=True)
    for command in value['cleanup']:
        # No-match is different from a failed primitive. Preserve the recovery
        # record and units if teardown is unconfirmed, rather than report success.
        result = subprocess.run(command, check=False, capture_output=True, text=True,
                                env={**os.environ, 'LC_ALL': 'C'})
        ok = result.returncode == 0
        if command[0] == 'pkill':
            ok = result.returncode in (0, 1)
            if ok:
                # Exit 1 also means matches existed but none could be signalled.
                # Exit 0 proves at least one signal, not that every task exited.
                wait_for_uid_exit(agent_uid)
        elif command[0] == 'crontab':
            ok = ok or (result.returncode == 1 and
                        (result.stderr or '').strip() == f"no crontab for {value['agent']}")
        elif command[0] == 'systemctl' and not ok:
            state = subprocess.run(['systemctl', 'show', command[-1],
                                    '--property=LoadState,ActiveState,UnitFileState'],
                                   check=False, capture_output=True, text=True)
            fields = dict(line.split('=', 1) for line in (state.stdout or '').splitlines() if '=' in line)
            ok = (state.returncode == 0 and fields.get('ActiveState') in ('inactive', 'failed')
                  and (fields.get('LoadState') == 'not-found' or
                       fields.get('UnitFileState') in ('disabled', 'static', 'masked')))
        if not ok:
            raise ValueError(f"Desktop cleanup failed ({command[0]}); provisioning retained for recovery")
    for name, content in value['files'].items():
        # Exclusive creation: never follow a planted link or replace existing system config.
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, 'w') as stream:
            stream.write(content)
    for name in value['remove']:
        Path(name).unlink(missing_ok=True)
    for command in value['finish']:
        subprocess.run(command, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('install', 'uninstall'))
    parser.add_argument('--owner', required=True, help='desktop login name (never root)')
    parser.add_argument('--apply', action='store_true', help='perform the reviewed plan as administrator')
    args = parser.parse_args()
    try:
        owner = pwd.getpwnam(args.owner)
        if owner.pw_uid < 1000 or owner.pw_uid == 65534:
            raise ValueError('Desktop owner must be an ordinary local login')
        value = plan(args.action, owner)
        if args.apply:
            apply_plan(value)
            value['apply'] = True
        print(json.dumps(value, indent=2))
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f'Setup refused: {error}\n')


if __name__ == '__main__':
    main()
