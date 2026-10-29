"""Phase-0 launcher pieces. Only `private_checkout` is used by the broker (container sessions' checkouts, through
acc.broker.containers); the probe stays a disconnected qualification payload. No provider qualification.

The boolean feature flag is deliberately explicit and defaults OFF. This API
only runs a fixed qualification payload, never an agent or arbitrary command.
Callers must own and exclusively control staging paths throughout execution;
path validation is not a replacement for the phase-1 descriptor-based launcher.
"""
import os
import re
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import BinaryIO


class PrototypeRefused(RuntimeError):
    """Fail closed without native execution or weaker sandbox fallback."""


def _directory(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    if (path == Path('/') or any(p.is_symlink() for p in (path, *path.parents))
            or not path.is_dir() or any(c in str(path) for c in ':,\n')):
        raise PrototypeRefused('requires an exclusive, non-symlink directory')
    return path


def probe_command(payload: Path, image: str, *, enabled: bool = False) -> list[str]:
    """Fixed offline probe; no images pulled, network grants or host sockets."""
    if not enabled:
        raise PrototypeRefused('container prototype disabled')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', image):
        raise PrototypeRefused('requires a locally reviewed image digest ID')
    payload = _directory(payload)
    if os.getuid() == 0:
        raise PrototypeRefused('root execution forbidden')
    uid, gid = os.getuid(), os.getgid()
    return [
        'podman', '--runtime=crun', 'run', '--rm', '--pull=never', '--network=none',
        '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges',
        '--userns=keep-id', f'--user={uid}:{gid}', '--pid=private', '--ipc=private',
        '--pids-limit=128', '--memory=512m', '--cpus=1', '--http-proxy=false',
        '--tmpfs=/tmp:rw,nosuid,nodev,size=128m,mode=1777',
        '--tmpfs=/run:rw,nosuid,nodev,noexec,size=16m,mode=755',
        f'--tmpfs=/run/credentials:rw,nosuid,nodev,noexec,size=1m,mode=700,uid={uid},gid={gid}',
        f'--volume={payload}:/qualification:ro', '--workdir=/tmp', '--interactive',
        '--entrypoint=/usr/bin/env', image, '-i', 'PATH=/usr/bin:/bin', 'HOME=/tmp',
        '/usr/bin/python3', '/qualification/probe.py',
    ]


def _git(cwd: Path, *args: str) -> None:
    # Do not inherit GIT_CONFIG_COUNT, alternates, templates or host credentials.
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null', GIT_TERMINAL_PROMPT='0')
    subprocess.run([
        'git', '--no-replace-objects', '-c', 'core.hooksPath=/dev/null',
        '-c', 'core.fsmonitor=false', '-c', 'gc.auto=0', '-c', 'maintenance.auto=false',
        '-c', 'protocol.allow=never', '-C', str(cwd), *args,
    ], env=env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)


def private_checkout(source: Path, destination: Path, branch: str = 'session', commit: str | None = None) -> None:
    """Seed only committed HEAD via bundle, never clone shared administration.

    Source is a broker-selected repository, not an arbitrary agent-supplied
    transport. Dirty files, private-ref import/backup and archive are phase 1.
    """
    if commit is not None and (not isinstance(commit, str) or not re.fullmatch('[0-9a-f]{40,64}', commit)):
        raise PrototypeRefused('invalid continuation commit')
    source = _directory(source)
    destination = Path(destination).absolute()
    _directory(destination.parent)
    try:
        destination.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise PrototypeRefused('destination already exists') from exc
    try:
        with tempfile.TemporaryDirectory(prefix='acc-ct0-git-') as scratch:
            bundle = Path(scratch) / 'seed.bundle'
            ref = 'HEAD'
            held_ref = None
            try:
                if commit:
                    ref = 'refs/acc/continuation-' + secrets.token_hex(16)
                    _git(source, 'update-ref', ref, commit, '0' * len(commit))
                    held_ref = ref
                _git(source, 'bundle', 'create', str(bundle), ref)
            finally:
                if held_ref:
                    # Compare-and-delete: never remove a ref another writer changed.
                    _git(source, 'update-ref', '-d', held_ref, commit)
            _git(destination, 'init', '--template=')
            _git(destination, '-c', 'protocol.file.allow=always', 'fetch', '--no-tags', str(bundle), ref)
            _git(destination, 'checkout', '-b', branch, 'FETCH_HEAD')
    except BaseException:
        shutil.rmtree(destination)
        raise


def send_credential(stream: BinaryIO, token: bytes, *, acc_issued: bool, expires_in: int) -> None:
    """Pipe an already narrowed Rynhult issued copy; no canonical auth-store access.

    This validates the transport envelope only, not a provider's token scope or
    expiry. Provider adapters must establish those properties before calling.
    There is intentionally no session-to-canonical credential writeback API.
    """
    if not acc_issued or not 0 < expires_in <= 3600 or not 0 < len(token) <= 65536:
        raise PrototypeRefused('requires bounded Rynhult issued ephemeral credentials')
    stream.write(token)
    stream.flush()
