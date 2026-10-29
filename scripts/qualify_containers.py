#!/usr/bin/env python3
"""Opt-in container qualification (phase 0 or owner-controlled phase 1); JSON to stdout."""
import argparse
import json
import os
import re
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from acc.broker.container_prototype import private_checkout

ROOT = Path(__file__).resolve().parents[1]
USERNS_SYSCTL = Path('/proc/sys/kernel/apparmor_restrict_unprivileged_userns')


def no_new_privs() -> str:
    return next(line.split(':')[1].strip() for line in Path('/proc/self/status').read_text().splitlines()
                if line.startswith('NoNewPrivs:'))


def user_manager_command(args: argparse.Namespace, uid: int) -> tuple[list[str], dict]:
    """Rerun this script as a transient service of the account's own (lingering) user manager: a new process tree
    outside the broker's NoNewPrivileges, where newuidmap/newgidmap can map the subordinate ids. Qualification only:
    production sessions go through the root launcher (deploy/containers/acc-containers), never the user manager."""
    runtime = f'/run/user/{uid}'
    env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': os.environ.get('HOME', '/'),
           'XDG_RUNTIME_DIR': runtime, 'DBUS_SESSION_BUS_ADDRESS': f'unix:path={runtime}/bus'}
    rest = ['--enable-prototype']
    if args.image:
        rest += ['--image', args.image]
        if args.image_archive:
            rest += ['--image-archive', str(args.image_archive.resolve())]
        for name in ('image_store', 'image_runroot'):
            value = getattr(args, name, None)
            if value:
                rest += ['--' + name.replace('_', '-'), str(value.resolve())]
    # PYTHONPATH: this checkout's acc package, not the one installed in the virtualenv
    return ['systemd-run', '--user', '--quiet', '--wait', '--pipe', '--collect', '--property=Delegate=yes',
            f'--working-directory={ROOT}', f'--setenv=PYTHONPATH={ROOT / "src"}', '--',
            sys.executable, str(Path(__file__).resolve()), *rest, '--launched-by-user-manager'], env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--enable-prototype', action='store_true', help='OFF by default')
    parser.add_argument('--image', help='optional locally reviewed sha256 image ID; never pulls')
    parser.add_argument('--image-archive', type=Path, help='owner-reviewed OCI archive loaded into scratch storage')
    parser.add_argument('--image-store', type=Path, help='explicit local rootless storage root (never pulls)')
    parser.add_argument('--image-runroot', type=Path, help='matching short rootless runtime path')
    parser.add_argument('--phase-1-plan', type=Path,
                        help='owner-reviewed plan; run as acc through the installed broker/trusted launcher')
    parser.add_argument('--user-manager', action='store_true',
                        help="run outside the broker's NoNewPrivileges, as a service of this account's lingering "
                             'user manager (needs: sudo loginctl enable-linger <account>)')
    parser.add_argument('--launched-by-user-manager', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT / 'scripts'))
    from container_qualification.phase1 import MANDATORY, result

    report = dict(status='disabled', go_phase_1=False, container_start_ms=None, container_memory_bytes=None,
                  checks={name: result('skip', 'phase-1 trusted-launcher qualification not requested')
                          for name in MANDATORY})
    if not args.enable_prototype:
        print(json.dumps(report, indent=2))
        return 2
    if os.getuid() == 0:
        report.update(status='refused', reason='Never run this qualification as root')
        print(json.dumps(report, indent=2))
        return 2
    if args.phase_1_plan:
        if args.user_manager or args.launched_by_user_manager or args.image or args.image_archive or args.image_store or args.image_runroot:
            report.update(status='refused', reason='phase 1 uses the trusted launcher, not phase-0 runtime options')
            print(json.dumps(report, indent=2))
            return 2
        # The standalone driver has the same imports both inside the image and from this checkout.
        sys.path.insert(0, str(ROOT / 'scripts/container_qualification'))
        from driver import qualify

        try:
            report = qualify(json.loads(args.phase_1_plan.read_text()))
        except (OSError, ValueError, KeyError) as error:
            report.update(status='refused', reason='phase-1 plan/context refused: ' + type(error).__name__)
        print(json.dumps(report, indent=2))
        return 0 if report['go_phase_1'] else 1
    if ((args.image_archive or args.image_store or args.image_runroot) and not args.image) or (args.image and not re.fullmatch(r'sha256:[0-9a-f]{64}', args.image)) or (args.image_archive and (args.image_store or args.image_runroot)):
        report.update(status='refused', reason='requires a pinned image ID; archive and explicit local store are mutually exclusive')
        print(json.dumps(report, indent=2))
        return 2
    if args.user_manager:
        command, env = user_manager_command(args, os.getuid())
        if not Path(env['XDG_RUNTIME_DIR'], 'bus').exists():
            report.update(status='refused', reason='no user manager for this account: an owner runs '
                                                    '`sudo loginctl enable-linger <account>` first')
            print(json.dumps(report, indent=2))
            return 2
        return subprocess.run(command, env=env).returncode
    try:
        userns_restricted = USERNS_SYSCTL.read_text().strip()
    except OSError:
        userns_restricted = None
    report.update(status='blocked', uid=os.getuid(),
                  launcher='user-manager' if args.launched_by_user_manager else 'direct',
                  apparmor_restrict_unprivileged_userns=userns_restricted,
                  apparmor=Path('/proc/self/attr/current').read_text().strip(),
                  cgroup=Path('/proc/self/cgroup').read_text().strip(),
                  no_new_privs=no_new_privs(),
                  deferred=['real Claude/Codex/Copilot pre-tool block and post-tool event',
                            'Codex hook trust and bundled bubblewrap', 'provider auth/refresh adapters',
                            'a real session through the trusted launcher (acc-containers) and its limits',
                            'NVIDIA Linux/WSL and private/LAN egress policy'])
    with tempfile.TemporaryDirectory(prefix='acc-ct0-') as scratch:
        base = Path(scratch)
        # No existing container/storage inventory, runtime sockets, or user config.
        env = {'PATH': os.environ['PATH'], 'XDG_CONFIG_HOME': str(base / 'config'),
               'XDG_DATA_HOME': str(base / 'data'), 'XDG_CACHE_HOME': str(base / 'cache'),
               'XDG_RUNTIME_DIR': str(base / 'runtime')}
        (base / 'runtime').mkdir(mode=0o700)
        engine = ['podman', '--root', str(base / 'storage'), '--runroot', str(base / 'run'),
                  '--tmpdir', str(base / 'tmp')]
        if args.image and not args.image_archive:
            engine = ['podman']
            if args.image_store:
                engine += ['--root', str(args.image_store.resolve())]
            if args.image_runroot:
                engine += ['--runroot', str(args.image_runroot.resolve())]
            env = {**os.environ, 'XDG_RUNTIME_DIR': f'/run/user/{os.getuid()}',
                   'CONTAINERS_CONF': '/dev/null', 'CONTAINERS_CONF_OVERRIDE': '/dev/null',
                   'CONTAINERS_STORAGE_CONF': '/dev/null'}
        # Empty storage.conf breaks Podman 4.9 keep-id reexec. This private
        # configuration fixes the backend and propagates explicit scratch paths.
        storage = '[storage]\ndriver = "overlay"\n'
        if not args.image or args.image_archive:
            storage += 'graphroot = ' + json.dumps(str(base / 'storage')) + '\n'
            storage += 'runroot = ' + json.dumps(str(base / 'run')) + '\n'
        else:
            if args.image_store:
                storage += 'graphroot = ' + json.dumps(str(args.image_store.resolve())) + '\n'
            if args.image_runroot:
                storage += 'runroot = ' + json.dumps(str(args.image_runroot.resolve())) + '\n'
        (base / 'storage.conf').write_text(storage)
        env['CONTAINERS_STORAGE_CONF'] = str(base / 'storage.conf')
        try:
            started = time.monotonic()
            probe = subprocess.run(engine + ['info', '--format=json'], env=env, capture_output=True,
                                   text=True, timeout=30)
            report['runtime_probe_ms'] = round((time.monotonic() - started) * 1000, 2)
            report['runtime_probe_peak_child_rss_kib'] = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
            report['rootless_info_exit'] = probe.returncode
            # Only the fixed runtime probe diagnostics are recorded; never payload stdout or secrets.
            report['rootless_info_error'] = probe.stderr[-3000:]
            if probe.returncode == 0:
                info = json.loads(probe.stdout)
                if not info['host']['security']['rootless']:
                    raise RuntimeError('runtime did not report rootless')
                report['status'] = 'rootless-info-only'
            started = time.monotonic()
            private_checkout(ROOT, base / 'checkout')
            report['private_checkout_ms'] = round((time.monotonic() - started) * 1000, 2)
            report['private_checkout_bytes'] = sum(p.stat().st_size for p in
                                                  (base / 'checkout').rglob('*') if p.is_file())
            if args.image and report.get('rootless_info_exit') == 0:
                if args.image_archive:
                    subprocess.run(engine + ['load', '--input', str(args.image_archive.resolve())],
                                   env=env, check=True, capture_output=True, timeout=120)
                sys.path.insert(0, str(ROOT / 'scripts/container_qualification'))
                from local import qualify
                report.update(qualify(engine, env, args.image, base))
                report['deferred'] = [name for name, item in report['checks'].items() if item['status'] != 'pass']
        except (OSError, subprocess.SubprocessError, RuntimeError, ValueError, KeyError) as exc:
            report['error_type'] = type(exc).__name__
        report.setdefault('reason', 'No-go: mandatory trusted-launcher evidence is not qualified')
    print(json.dumps(report, indent=2))
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
