"""Image-only Option A policy. No environment or repository discovery is trusted.

The launcher binds its generated contract at this reserved read-only location.
The agent has no mount capability and cannot replace its parent or the rootfs.
This authenticates the contract within the launcher-managed mount namespace;
it is not attestation against a host administrator controlling that namespace.
"""
import json
import os
import socket
import stat
import sys
from pathlib import Path

CONTRACT = Path('/run/acc-container/contract.json')
INITIAL_MAP = ['0', '0', '4294967295']


def root_uids():
    """The uids root's files show as here: 0, and in a user namespace also the kernel's overflow uid. The launcher's
    session container is rootless, and host root is never mapped in a rootless user namespace, so the root-owned
    rootfs and contract show as 65534 there (qualification attempt 15). Never in the initial namespace, where 65534 is
    the real nobody, and never the caller's own uid. Inside, a mapped uid equal to the overflow uid looks the same;
    nothing in the container can take it (no capabilities, no new privileges), and the paths checked with this are in
    the launcher-checked root-owned rootfs or its read-only contract bind, never in a folder the account writes."""
    uids = {0}
    try:
        if Path('/proc/self/uid_map').read_text().split() != INITIAL_MAP:
            overflow = int(Path('/proc/sys/kernel/overflowuid').read_text())
            if overflow != os.getuid():
                uids.add(overflow)
    except (OSError, ValueError):
        pass
    return uids


def container_is_sandbox():
    try:
        parent = CONTRACT.parent.lstat()
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid not in root_uids() or parent.st_mode & 0o022
                or not os.statvfs('/').f_flag & os.ST_RDONLY
                or not os.statvfs(CONTRACT.parent).f_flag & os.ST_RDONLY):
            return False
        fd = os.open(CONTRACT, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_size > 4096 or info.st_mode & 0o222
                    or not os.fstatvfs(fd).f_flag & os.ST_RDONLY):
                return False
            value = json.loads(os.read(fd, 4097))
        finally:
            os.close(fd)
        status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
        # The contract states network none; observe it too. A copied contract in a container
        # someone else started with a network must not turn the inner sandboxes off.
        return (value.get('version') == 1 and value.get('sandbox') == 'container' and value.get('network') == 'none'
                and value.get('issuer') == 'acc-containers' and status['NoNewPrivs'].strip() == '1'
                and status['Seccomp'].strip() == '2' and int(status['CapBnd'].strip(), 16) == 0
                and {name for _, name in socket.if_nameindex()} == {'lo'})
    except (OSError, ValueError, KeyError, AttributeError):
        return False


def read_only(path):
    return bool(os.statvfs(path).f_flag & os.ST_RDONLY)


def fixed(path):
    """Root's, on the read-only rootfs, and nothing on the way to it can be changed by the session."""
    owners = root_uids()
    for part in (path, *path.parents):
        info = part.lstat()
        if info.st_uid not in owners or info.st_mode & 0o022 or stat.S_ISLNK(info.st_mode):
            return False
    return read_only(path)


# Copilot (1.0.102) reads hooks from $COPILOT_HOME/hooks only. Its machine policy hooks (/etc/github-copilot/policy.d),
# which nothing can switch off, are skipped unless uid 0 owns them, and root's files show as the overflow uid here
# (attempt 16: no hook ran at all). It rewrites config.json in $COPILOT_HOME at every start, so that folder is the
# session's tmpfs and its hooks are a link to the image's read-only copy. It loads hooks once, at start.
COPILOT_HOME = Path('/tmp/acc-state/copilot')
COPILOT_HOOKS = Path('/home/acc/.copilot/hooks')
COPILOT_MAIN = '/opt/acc/copilot/cli-main.js'
COPILOT_GUARD = '/usr/local/bin/acc-guard copilot PreToolUse'
COPILOT_REFUSED = 78
# The settings check the broker also runs for native Copilot (src/acc/providers/copilot_settings.py), loaded by its fixed
# image path, and only once it is root's and read-only like this file.
COPILOT_SETTINGS = Path('/opt/acc/src/acc/providers/copilot_settings.py')


def copilot_settings():
    import runpy

    if not fixed(COPILOT_SETTINGS) or not stat.S_ISREG(COPILOT_SETTINGS.lstat().st_mode):
        raise ValueError('the image settings check is not intact')
    return runpy.run_path(str(COPILOT_SETTINGS))


def copilot_problem(args, *, cwd=None, environ=None):
    """Why Copilot would start without Rynhult's hooks, or None. Checked before every start; Copilot never starts
    when there is one (fail closed). Only `--version` runs no session and is let through."""
    args = list(args)
    if args == ['--version']:
        return None
    environ = os.environ if environ is None else environ
    if environ.get('COPILOT_HOME') != str(COPILOT_HOME):
        return f'COPILOT_HOME must be {COPILOT_HOME}'
    for value in args:
        if value == '--':
            break
        if value == '--config-dir' or value.startswith('--config-dir='):
            return '--config-dir would move its hooks'
    try:
        home = COPILOT_HOME.lstat()
        if not stat.S_ISDIR(home.st_mode) or home.st_uid != os.getuid():
            return f'{COPILOT_HOME} is not the session folder'
        hooks = COPILOT_HOOKS / 'acc.json'
        if not fixed(COPILOT_HOOKS) or not fixed(hooks) or not stat.S_ISREG(hooks.lstat().st_mode) \
                or sorted(os.listdir(COPILOT_HOOKS)) != ['acc.json']:
            return f"the image's {hooks} is not intact"
        entries = json.loads(hooks.read_text()).get('hooks', {}).get('PreToolUse')
        if not isinstance(entries, list) or not any(isinstance(e, dict) and e.get('bash') == COPILOT_GUARD
                                                    for e in entries):
            return f'{hooks} does not run the guard before tools'
        link = COPILOT_HOME / 'hooks'
        if not os.path.lexists(link):
            link.symlink_to(COPILOT_HOOKS)
        if not link.is_symlink() or os.readlink(link) != str(COPILOT_HOOKS):
            return f"{link} is not the link to the image's hooks"
    except (OSError, ValueError, AttributeError):
        return "Copilot's hooks can't be checked"
    try:
        shared = copilot_settings()
    except (OSError, ValueError):
        return "the image's Copilot settings check can't be loaded"
    own = [COPILOT_HOME / 'settings.json', COPILOT_HOME / 'settings.local.json', COPILOT_HOME / 'config.json']
    return (next((problem for problem in map(shared['settings_problem'], own) if problem), None)
            or shared['repository_problem'](Path(os.getcwd() if cwd is None else cwd)))


def require_container():
    """The image's managed Claude policy disables its inner sandbox (Option A)."""
    if not container_is_sandbox():
        raise RuntimeError('unverified container sandbox; refusing the image payload')


def arguments(kind, args):
    args = list(args)
    if kind == 'copilot':
        return [COPILOT_MAIN, *args]
    if kind not in ('chrome', 'codex'):
        raise ValueError('unknown container CLI')
    # Every Codex in the image keeps hooks on: a repository's .codex/config.toml could
    # otherwise turn them off. Rynhult's own last override repeats it after any later -c.
    hooks = ['-c', 'features.hooks=true'] if kind == 'codex' else []
    if not container_is_sandbox():
        return [*hooks, *args]
    if kind == 'chrome':
        return ['--no-sandbox', *args]
    # Preserve approval settings. Replace the sandbox options emitted by Rynhult,
    # and set the default for CLI paths without an explicit sandbox option.
    for index, value in enumerate(args):
        if value == '--':
            break
        if value in ('--sandbox', '-s') and index + 1 < len(args):
            args[index + 1] = 'danger-full-access'
        elif value.startswith('--sandbox='):
            args[index] = '--sandbox=danger-full-access'
        elif value.startswith('-s') and not value.startswith('--') and len(value) > 2:
            args[index] = '-sdanger-full-access'
    return [*hooks, '-c', 'sandbox_mode="danger-full-access"', *args]


def execute(kind, executable, args):
    if kind == 'copilot':
        problem = copilot_problem(args)
        if problem:
            sys.stderr.write(f"Rynhult: Copilot's hooks can't be verified, so it won't start: {problem}\n")
            raise SystemExit(COPILOT_REFUSED)
        # The pinned package is unpacked during the build, not into writable session cache.
        os.environ['COPILOT_AUTO_UPDATE'] = 'false'
    os.execv(executable, [executable, *arguments(kind, args)])
