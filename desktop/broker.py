"""Packaged desktop broker configuration / Linux systemd entry point: run the shared broker as the dedicated account."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# -I ignores PYTHONPATH/user site. Both paths belong to the root-owned package.
RESOURCES = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RESOURCES / 'src'))
sys.path.insert(0, str(RESOURCES / 'desktop'))

from execution import read_manifest, validate_account  # noqa: E402

from acc.broker.config import BrokerConfig  # noqa: E402
from acc.broker.server import serve  # noqa: E402


def configuration(owner_uid: int):
    value = read_manifest(owner_uid)
    if value is None:
        raise ValueError('Desktop execution is not provisioned')
    account = validate_account(value, owner_uid)
    if os.getuid() != account.pw_uid or os.geteuid() == 0:
        raise ValueError('Desktop broker must run as its provisioned agent account')
    if sys.platform != 'darwin' and (os.environ.get('LISTEN_PID') != str(os.getpid()) or os.environ.get('LISTEN_FDS') != '1'):
        raise ValueError('Desktop broker requires its protected systemd socket')
    home = Path(value['home'])
    # Never inherit the desktop owner's transport capability or live session variables.
    activation = {} if sys.platform == 'darwin' else {key: os.environ[key] for key in ('LISTEN_PID', 'LISTEN_FDS')}
    tools = '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin' if sys.platform == 'darwin' else '/usr/local/bin:/usr/bin:/bin'
    os.environ.clear()
    if sys.platform == 'darwin':  # tmux -L off /private/tmp, into the agent's run directory (mac_tmux.py)
        from mac_tmux import LABEL, tmpdir
        activation['TMUX_TMPDIR'] = str(tmpdir(value))
    os.environ.update(activation, HOME=str(home), PATH=f'{home}/.local/bin:{RESOURCES}/bin:{tools}',
                      # PTY attach starts a fresh `python -m acc.broker.pty_exec`.
                      # The bundled runtime has no acc install; only this protected source tree may supply it.
                      PYTHONPATH=str(RESOURCES / 'src'),
                      TERM='tmux-256color', SHELL='/bin/sh', LANG='en_US.UTF-8' if sys.platform == 'darwin' else 'C.UTF-8', PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1')
    paths = {name: str(home / '.local/bin' / name) if (home / '.local/bin' / name).is_file() else name
             for name in ('claude', 'codex')}
    paths.update({name: '/nonexistent/desktop-provider-unavailable' for name in ('copilot', 'antigravity', 'cursor', 'opencode')})
    paths['gh'] = 'gh'
    return BrokerConfig(socket_path=Path(value['socket']), run_dir=Path(value['run_dir']) / value['agent'],
                        tmux_socket=LABEL if sys.platform == 'darwin' else f'rynhult-desktop-{owner_uid}', allowed_roots=(Path(value['projects']),),
                        allowed_uids=frozenset({owner_uid}), providers=paths,
                        hook_path=str(RESOURCES / 'bin/acc-hook'), catalog_dir=RESOURCES / 'catalog',
                        catalog_local_dir=home / 'catalog-local')


if __name__ == '__main__':
    if sys.platform != 'linux':
        raise ValueError('Use the installed macOS launchd entry point')
    asyncio.run(serve(configuration(int(sys.argv[1]))))
