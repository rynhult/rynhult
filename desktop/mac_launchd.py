"""The owner's opt-in start-at-login LaunchAgent for the desktop app, generated and checked here.

    python3 desktop/mac_launchd.py plan --app "/Applications/Rynhult Desktop.app"    # JSON dry run (any OS)
    python3 desktop/mac_launchd.py install --app ... --apply                         # macOS, as the owner
    python3 desktop/mac_launchd.py uninstall --apply

The agent opens the app's own executable (which starts the portable main as its companion) only in the owner's
graphical (Aqua) session, at login, once. Quitting the app stays quitting: it isn't restarted (no KeepAlive). The
plist lives in ~/Library/LaunchAgents, owned by the owner and writable by nobody else, and is loaded with
`launchctl bootstrap gui/<uid>`. Never root: a LaunchAgent belongs to one person's login. Installing needs the
macOS standalone gate (mac_gate.require_ready); generating and planning don't.

The broker is a separate root-owned LaunchDaemon running as the agent account (mac_setup.py); nothing here touches it.
"""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import pwd
import re
import stat
import subprocess
import sys
from pathlib import Path

LABEL = 'com.rynhult.desktop.app'
BUNDLE_ID = 'com.rynhult.desktop'
LAUNCHCTL = '/bin/launchctl'
EXECUTABLE = re.compile(r'^/(?:Applications|Users/[^/]+/Applications)/[^/]+\.app/Contents/MacOS/[^/]+$')


def plist_path(owner) -> Path:
    return Path(owner.pw_dir) / 'Library/LaunchAgents' / f'{LABEL}.plist'


def logs(owner) -> Path:
    return Path(owner.pw_dir) / 'Library/Logs/Rynhult Desktop'


def executable_for(app: Path) -> Path:
    """The app's main executable, from its Info.plist (CFBundleExecutable), checked to stay inside the app."""
    app = Path(os.path.normpath(app))
    info = app / 'Contents/Info.plist'
    with info.open('rb') as stream:
        name = plistlib.load(stream).get('CFBundleExecutable')
    if not isinstance(name, str) or '/' in name or name in ('', '.', '..'):
        raise ValueError('The app has no usable CFBundleExecutable')
    return check_executable(app / 'Contents/MacOS' / name)


def check_executable(path: Path) -> Path:
    text = os.path.normpath(str(path))
    if not EXECUTABLE.fullmatch(text):
        raise ValueError('Start-at-login opens only an app installed in /Applications or ~/Applications')
    return Path(text)


def agent(owner, executable: Path) -> dict:
    executable = check_executable(executable)
    folder = logs(owner)
    return {
        'Label': LABEL,
        'ProgramArguments': [str(executable), '--started-at-login'],
        'RunAtLoad': True,
        'KeepAlive': False,  # quitting is final until the next login
        'LimitLoadToSessionType': ['Aqua'],  # a graphical login only: not SSH, not the login window
        'ProcessType': 'Interactive',
        'AssociatedBundleIdentifiers': [BUNDLE_ID],  # System Settings shows it under the app
        'ThrottleInterval': 30,
        'Umask': 0o077,
        'WorkingDirectory': owner.pw_dir,
        'EnvironmentVariables': {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LANG': 'en_US.UTF-8'},
        'StandardOutPath': str(folder / 'app.log'),
        'StandardErrorPath': str(folder / 'app.log'),
    }


def render(value: dict) -> bytes:
    return plistlib.dumps(value, fmt=plistlib.FMT_XML, sort_keys=True)


def check_installed(path: Path, owner) -> None:
    """The plist and its folder belong to the owner and nobody else can change them; never a symlink."""
    for item in (path, path.parent):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != owner.pw_uid or info.st_mode & 0o022:
            raise ValueError(f'{item} must belong to {owner.pw_name}, not be a link, and not be writable by others')


def commands(owner, action: str) -> list[list[str]]:
    domain = f'gui/{owner.pw_uid}'
    path = str(plist_path(owner))
    if action == 'install':
        # bootout first so a changed plist replaces the loaded one; its failure (not loaded) is expected.
        return [[LAUNCHCTL, 'bootout', f'{domain}/{LABEL}'], [LAUNCHCTL, 'bootstrap', domain, path]]
    if action == 'uninstall':
        return [[LAUNCHCTL, 'bootout', f'{domain}/{LABEL}']]
    raise ValueError('Invalid action')


def plan(owner, action: str, executable: Path | None = None) -> dict:
    out = {'action': action, 'label': LABEL, 'plist': str(plist_path(owner)), 'commands': commands(owner, action)}
    if action == 'install':
        out['agent'] = agent(owner, executable)
        out['logs'] = str(logs(owner))
    return out


def write_private(path: Path, data: bytes, owner) -> None:
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    (logs(owner)).mkdir(mode=0o700, parents=True, exist_ok=True)
    scratch = path.with_name(f'.{path.name}.new')
    scratch.unlink(missing_ok=True)
    fd = os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(scratch, path)
    check_installed(path, owner)


def preflight(owner, gate=None) -> None:
    """macOS, the owner themselves (never root), and the macOS standalone gate."""
    if sys.platform != 'darwin':
        raise ValueError('LaunchAgents can be installed only on macOS; use plan elsewhere')
    if os.geteuid() == 0 or os.getuid() != owner.pw_uid:
        raise ValueError('Run as the owner, never as root: a LaunchAgent belongs to one login')
    from mac_gate import require_ready
    (gate or require_ready)(owner.pw_uid)


def apply(owner, action: str, executable: Path | None = None, *, run=subprocess.run, gate=None) -> dict:
    preflight(owner, gate)
    value = plan(owner, action, executable)
    path = plist_path(owner)
    env = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'}
    if action == 'install':
        write_private(path, render(value['agent']), owner)
        run(value['commands'][0], capture_output=True, env=env)  # not loaded yet: fine
        run(value['commands'][1], check=True, capture_output=True, env=env)
    else:
        run(value['commands'][0], capture_output=True, env=env)
        if path.is_symlink() or path.exists():
            check_installed(path, owner)
            path.unlink()
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('action', choices=('plan', 'install', 'uninstall'))
    parser.add_argument('--app', type=Path, help='the installed .app (install and plan)')
    parser.add_argument('--apply', action='store_true', help='make the change (macOS only); default is a dry run')
    args = parser.parse_args(argv)
    owner = pwd.getpwuid(os.getuid())
    action = 'install' if args.action == 'plan' else args.action
    try:
        if args.apply and args.action != 'plan':
            preflight(owner)  # refuse before looking at the app
        if action == 'install' and args.app is None:
            raise ValueError('--app names the installed .app')
        executable = executable_for(args.app) if action == 'install' else None
        result = apply(owner, action, executable) if args.apply and args.action != 'plan' else plan(owner, action, executable)
    except (ValueError, OSError, RuntimeError, subprocess.CalledProcessError) as error:  # RuntimeError: mac_gate.Blocked
        print(f'mac_launchd: {error}', file=sys.stderr)
        return 1
    print(json.dumps(result, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
