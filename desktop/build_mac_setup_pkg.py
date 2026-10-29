"""Build a one-time, owner-bound macOS execution Installer package.

Default: portable JSON plan, no writes. --build needs macOS, a bundled macOS
runtime and desktop/web-dist. Does not build/sign the separate Tauri window.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'desktop'))
from mac_setup import PREFIX, RESOURCES, plan  # noqa: E402

MODULES = ('main.py', 'execution.py', 'broker.py', 'setup.py', 'mac_setup.py', 'mac_broker.py',
           'mac_gate.py', 'mac_keychain.py', 'mac_launchd.py', 'mac_tmux.py')


def python_wrapper(resources: Path, script: str) -> str:
    return '#!/bin/sh\nexec ' + shlex.join([str(resources / 'desktop/runtime/python/bin/python3'), '-I',
                                         str(resources / 'bin' / (script + '.py'))]) + ' "$@"\n'


def preinstall_script(action: str, prefix: Path = PREFIX) -> str:
    script = '#!/bin/sh\nset -eu\n[ "${3:-/}" = / ] || { echo "Refusing a non-startup target volume" >&2; exit 1; }\n'
    if action == 'install':
        config = shlex.quote(str(prefix / 'Config'))
        script += f'for receipt in {config}/*/identity.json {config}/*/execution.json; do\n'
        script += '  if [ -e "$receipt" ] || [ -L "$receipt" ]; then echo "Existing desktop receipt: uninstall/recover before replacing runtime" >&2; exit 1; fi\ndone\n'
    return script


def build_plan(owner: str, uid: int, gid: int, agent_id: int, runtime: Path, output: Path, version: str, *,
               action='install', purge=False, remove_runtime=False) -> dict:
    plan(action, SimpleNamespace(pw_name=owner, pw_uid=uid, pw_gid=gid), agent_id, purge=purge)
    if remove_runtime and not (action == 'uninstall' and purge):
        raise ValueError('Removing runtime requires explicit agent purge')
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('Use a numeric major.minor.patch package version')
    command = [str(RESOURCES / 'desktop/runtime/python/bin/python3'), '-I', str(RESOURCES / 'desktop/mac_setup.py'),
               action, '--owner', owner, '--owner-uid', str(uid), '--owner-gid', str(gid),
               '--agent-id', str(agent_id), '--apply']
    if purge:
        command += ['--purge-agent', '--confirm-agent', f'rynd-{uid}']
    if remove_runtime:
        command += ['--remove-runtime']
    postinstall = '#!/bin/sh\nset -eu\n[ "${3:-/}" = / ] || { echo "Refusing a non-startup target volume" >&2; exit 1; }\nexec ' + shlex.join(command) + '\n'
    return dict(build=False, action=action, owner=owner, owner_uid=uid, owner_gid=gid, agent_id=agent_id,
                purge=purge, remove_runtime=remove_runtime, payload=action == 'install', installed_resources=str(RESOURCES), runtime=str(runtime), postinstall=postinstall, preinstall=preinstall_script(action),
        pkgbuild=['/usr/bin/pkgbuild', *(['--root', '<private-stage>/payload'] if action == 'install' else ['--nopayload']),
                  '--scripts', '<private-stage>/scripts',
                  '--identifier', f'com.rynhult.desktop.execution.{uid}' if action == 'install' else f'com.rynhult.desktop.uninstall.{uid}', '--version', version,
                  '--ownership', 'recommended', '--install-location', '/', str(output)])


def recipe(value: dict) -> dict:
    return {key: value[key] for key in ('action', 'owner', 'owner_uid', 'owner_gid', 'agent_id', 'purge', 'remove_runtime')}


def verify_expanded(expanded: Path, value: dict) -> None:
    scripts = expanded / 'Scripts'
    expected = {'preinstall', 'postinstall', 'mac-plan.json'}
    if set(p.name for p in scripts.iterdir()) != expected or any(p.is_symlink() for p in scripts.iterdir()):
        raise ValueError('Expected only the reviewed postinstall and recipe')
    if (json.loads((scripts / 'mac-plan.json').read_text()) != recipe(value)
            or (scripts / 'postinstall').read_text() != value['postinstall']
            or (scripts / 'preinstall').read_text() != value['preinstall']):
        raise ValueError('Package action/owner or postinstall differs from the reviewed plan')


def verify_package(package: Path, value: dict) -> None:
    if sys.platform != 'darwin':
        raise ValueError('Package expansion verification requires macOS pkgutil')
    with tempfile.TemporaryDirectory(prefix='rynhult-pkg-review-') as temporary:
        expanded = Path(temporary) / 'expanded'
        subprocess.run(['/usr/sbin/pkgutil', '--expand-full', str(package.resolve()), str(expanded)], check=True)
        verify_expanded(expanded, value)


def stage_resources(destination: Path, runtime: Path) -> None:
    destination.mkdir(parents=True)
    for name in ('LICENSE', 'THIRD_PARTY_NOTICES.md'):
        shutil.copy2(ROOT / name, destination / name)
    desktop = destination / 'desktop'
    desktop.mkdir()
    for name in MODULES:
        shutil.copy2(ROOT / 'desktop' / name, desktop / name)
    shutil.copytree(runtime, desktop / 'runtime', symlinks=True)
    shutil.copytree(ROOT / 'desktop/web-dist', desktop / 'web-dist')
    shutil.copytree(ROOT / 'src/acc', destination / 'src/acc', ignore=shutil.ignore_patterns('static', '__pycache__'))
    shutil.copytree(ROOT / 'catalog', destination / 'catalog', ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copytree(ROOT / 'docs/wiki', destination / 'docs/wiki')
    binaries = destination / 'bin'
    binaries.mkdir()
    shutil.copy2(ROOT / 'deploy/lib/acc-env.sh', binaries / 'acc-env.sh')
    shutil.copy2(ROOT / 'src/acc/env.py', binaries / 'acc_env.py')
    shutil.copy2(ROOT / 'src/acc/tooling.py', binaries / 'acc-tool-policy.py')
    for source in (ROOT / 'bin').glob('acc-*'):
        text = source.read_text()
        if 'python' in text.splitlines()[0]:
            # Kernel shebang parsing cannot represent the spaces in the runtime path.
            shutil.copy2(source, binaries / (source.name + '.py'))
            (binaries / source.name).write_text(python_wrapper(RESOURCES, source.name))
        else:
            shutil.copy2(source, binaries / source.name)
        (binaries / source.name).chmod(0o755)
    for path in [destination, *destination.rglob('*')]:
        if path.is_symlink():
            if not path.resolve().is_relative_to(destination):
                raise ValueError('Runtime symlink escapes the staged package')
            continue
        mode = path.stat().st_mode
        path.chmod((mode | 0o444 | (0o111 if path.is_dir() else 0)) & ~0o022)


def build(value: dict) -> None:
    if sys.platform != 'darwin' or os.geteuid() == 0:
        raise ValueError('Build as the ordinary owner on macOS; Installer authorizes setup later')
    if value['payload'] and not (ROOT / 'desktop/web-dist/index.html').is_file():
        raise ValueError('Build the desktop UI into desktop/web-dist first')
    runtime = Path(value['runtime']).resolve()
    executable = runtime / 'python/bin/python3'
    if value['payload']:
        subprocess.run([str(executable), '-I', '-c',
                    "import sys, fastapi, uvicorn, sqlalchemy; assert sys.platform == 'darwin' and sys.version_info >= (3,12)"], check=True)
    scratch = ROOT / '.acc/build'
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='mac-pkg-', dir=scratch) as temporary:
        stage = Path(temporary)
        payload, scripts = stage / 'payload', stage / 'scripts'
        scripts.mkdir()
        payload.mkdir()
        if value['payload']:
            target = payload / RESOURCES.relative_to('/')
            stage_resources(target, runtime)
        preinstall = scripts / 'preinstall'
        preinstall.write_text(value['preinstall'])
        preinstall.chmod(0o755)
        postinstall = scripts / 'postinstall'
        postinstall.write_text(value['postinstall'])
        postinstall.chmod(0o755)
        (scripts / 'mac-plan.json').write_text(json.dumps(recipe(value), indent=2) + '\n')
        argv = [arg.replace('<private-stage>', str(stage)) for arg in value['pkgbuild']]
        Path(argv[-1]).parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(argv, check=True)
    value['build'] = True


def main():
    import pwd
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--owner', required=True)
    parser.add_argument('--owner-uid', type=int)
    parser.add_argument('--owner-gid', type=int)
    parser.add_argument('--agent-id', type=int, default=50000)
    parser.add_argument('--runtime', type=Path, default=ROOT / 'desktop/runtime')
    parser.add_argument('--out', type=Path, default=ROOT / 'desktop/package/Rynhult-Execution.pkg')
    parser.add_argument('--version', default='0.0.0')
    parser.add_argument('--build', action='store_true')
    parser.add_argument('--verify', type=Path, help='expand a package on macOS and check its action/owner recipe')
    parser.add_argument('--uninstall', action='store_true')
    parser.add_argument('--purge-agent', action='store_true')
    parser.add_argument('--remove-runtime', action='store_true')
    args = parser.parse_args()
    try:
        if (args.owner_uid is None) != (args.owner_gid is None):
            raise ValueError('Provide both UID and GID for a portable owner plan')
        owner = pwd.getpwnam(args.owner) if args.owner_uid is None else SimpleNamespace(pw_uid=args.owner_uid, pw_gid=args.owner_gid)
        value = build_plan(args.owner, owner.pw_uid, owner.pw_gid, args.agent_id, args.runtime, args.out, args.version,
                           action='uninstall' if args.uninstall else 'install', purge=args.purge_agent, remove_runtime=args.remove_runtime)
        if args.verify:
            verify_package(args.verify, value)
        if args.build:
            build(value)
        print(json.dumps(value, indent=2))
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, f'macOS package refused: {error}\n')


if __name__ == '__main__':
    main()
