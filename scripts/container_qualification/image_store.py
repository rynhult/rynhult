"""Owner-invoked host probe: run as the agent, outside all containers; writes no image bytes."""
import errno
import json
import os
import pwd
import re
import shlex
import sqlite3
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

STORE = Path('/var/lib/acc/session-images')
ACCOUNT_PROFILE = Path(__file__).resolve().parents[2] / 'deploy/containers/account-store.conf'


def account_store(home):
    """The account's default graphroot: what the session storage.conf searches before the protected store."""
    return Path(home) / '.local/share/containers/storage'


# Podman records its run root and tmp dir in the store's database on first use and refuses to start with any other
# ("database run root ... does not match our run root"). Podman 4.9 keeps them in <graphroot>/libpod/db.sql (SQLite,
# the default since 4.8) or, for a store an older Podman made, in libpod/bolt_state.db, which this can't read.
# The account writes its database, and these values land in commands an owner may paste in a root shell. Plain path
# characters only (no C1 controls or bidi marks that make a printed command read differently from what runs), already
# normalized (no ..), and inside the account's own runtime places or home, where Podman puts them (kit review M-1).
RECORDED_PATH = re.compile(r'/[A-Za-z0-9._@+=,:/-]{0,1023}')


def account_path(value, uid, home):
    """Whether a recorded run root or tmp dir is a plain path in one of the account's own places."""
    if not isinstance(value, str) or not RECORDED_PATH.fullmatch(value) or os.path.normpath(value) != value:
        return False
    places = (f'/run/user/{uid}', f'/tmp/storage-run-{uid}', f'/tmp/podman-run-{uid}', f'/tmp/containers-user-{uid}',
              os.path.normpath(str(home)))
    return any(value.startswith(place.rstrip('/') + '/') for place in places)


def recorded_paths(store, uid, home=None):
    """(run root, tmp dir, how they were found) for the cleanup commands: the database's, or Podman's defaults for a
    store it has no database for yet; (None, None, why) when they can't be known. The account writes its database,
    so only a plain absolute path is ever put in a command."""
    libpod = Path(store) / 'libpod'
    if (libpod / 'bolt_state.db').exists():
        return None, None, 'this store has an older Podman database (BoltDB), which this check cannot read'
    if not (libpod / 'db.sql').exists():
        return f'/run/user/{uid}/containers', None, "Podman's defaults (it has recorded none for this store yet)"
    try:
        db = sqlite3.connect(f'file:{(libpod / "db.sql").as_posix()}?mode=ro', uri=True, timeout=5)
        try:
            row = db.execute('SELECT RunRoot, TmpDir FROM DBConfig WHERE ID = 1').fetchone()
        finally:
            db.close()
    except sqlite3.Error:
        return None, None, "this store's Podman database could not be read"
    home = Path(store).parents[3] if home is None else home  # <home>/.local/share/containers/storage
    if not row or not all(account_path(value, uid, home) for value in row):
        return None, None, ("this store's Podman database records a run root or tmp dir that is not a plain path in "
                            "this account's runtime directory or home")
    return row[0], row[1], 'the run root and tmp dir Podman recorded for this store'


def _records(path):
    try:
        value = json.loads(path.read_bytes())
    except FileNotFoundError:
        return []
    if not isinstance(value, list) or not all(isinstance(item, dict) and isinstance(item.get('id'), str) for item in value):
        raise ValueError(f'unreadable storage metadata {path}')
    return value


def _chain(layers, top):
    seen = []
    while isinstance(top, str) and top in layers and top not in seen:
        seen.append(top)
        top = layers[top].get('parent')
    return seen


def shadowing(image, home, protected=None):
    """Layers of the pinned session image that also exist in the account's own store, where Podman finds them first
    (qualification attempt 11: a build's intermediate stage left one there). Reads containers/storage metadata only.
    Keep-id copies are new layers with new IDs, and the launcher keeps them in a per-launch tmpfs: never matched."""
    protected = Path(protected or STORE)
    pinned = next((i for i in _records(protected / 'overlay-images/images.json') if i['id'] == image[7:]), None)
    if pinned is None:
        raise ValueError('pinned image is not in the protected store')
    wanted = set(_chain({layer['id']: layer for layer in _records(protected / 'overlay-layers/layers.json')},
                        pinned.get('layer')))
    if not wanted:
        raise ValueError('pinned image layers are not in the protected store')
    own = account_store(home)
    layers = {layer['id']: layer for name in ('layers.json', 'volatile-layers.json')
              for layer in _records(own / 'overlay-layers' / name)}
    found = sorted(wanted & set(layers))
    own_images = _records(own / 'overlay-images/images.json')
    own_containers = _records(own / 'overlay-containers/containers.json')

    def holds(record):
        return bool(set(_chain(layers, record.get('layer'))) & set(found))

    # An account image with the pinned ID, whatever its layers, is also found before the protected copy.
    reused = any(i['id'] == image[7:] for i in own_images)
    images = [(len(_chain(layers, i.get('layer'))), i['id']) for i in own_images if holds(i) or i['id'] == image[7:]]
    containers = sorted(c['id'] for c in own_containers if holds(c) or c.get('image') == image[7:])
    images = [i for _, i in sorted(images, reverse=True)]  # children before the images they're built on
    # The account writes this metadata: name only full hex IDs (never a flag, a name, shell syntax or a line break).
    malformed = sum(not re.fullmatch(r'[0-9a-f]{64}', i) for i in images + containers)
    held = {layer for i in own_images if i['id'] in images for layer in _chain(layers, i.get('layer'))}
    held |= {layer for c in own_containers if c['id'] in containers for layer in _chain(layers, c.get('layer'))}
    named = [i for i in images if re.fullmatch(r'[0-9a-f]{64}', i)]
    return {'layers': found, 'images': named, 'containers': [c for c in containers if re.fullmatch(r'[0-9a-f]{64}', c)],
            'malformed': malformed, 'unreferenced': sorted(set(found) - held), 'reused_id': reused,
            'store': str(own), **dict(zip(('runroot', 'tmpdir', 'paths'), recorded_paths(own, os.getuid(), home)))}


def shadowed(found):
    return bool(found['layers'] or found['reused_id'])


def cleanup_commands(found):
    """Exactly the account's images and containers that hold those layers or reuse the pinned ID: nothing else in its
    store. Absolute paths only (never $HOME), so the commands address the store that was checked in any shell. None
    when the run root Podman recorded can't be known: Podman would refuse them."""
    if not found.get('runroot'):
        return []
    tmpdir = f' --tmpdir {shlex.quote(found["tmpdir"])}' if found.get('tmpdir') else ''
    prefix = ('CONTAINERS_CONF=/dev/null CONTAINERS_CONF_OVERRIDE=/dev/null '
              f'{shlex.quote(f"CONTAINERS_STORAGE_CONF={ACCOUNT_PROFILE}")} podman '
              f'--root {shlex.quote(found["store"])} --runroot {shlex.quote(found["runroot"])}{tmpdir}')
    commands = []
    if found['containers']:
        commands.append(f'{prefix} rm -- ' + ' '.join(map(shlex.quote, found['containers'])))
    if found['images']:
        commands.append(f'{prefix} image rm -- ' + ' '.join(map(shlex.quote, found['images'])))
    return commands


def explain(image, found):
    lines = []
    if found['layers']:
        lines.append(f"{len(found['layers'])} layer(s) of {image} also exist in this account's own store "
                     f"{found['store']}, where Podman finds them before the protected copy, so the launcher refuses the "
                     'session.')
    if found['reused_id']:
        lines.append(f"This account's own store {found['store']} has an image with the pinned ID {image}; Podman "
                     'finds it before the protected copy, so the launcher refuses the session.')
    commands = cleanup_commands(found)
    if commands:
        lines.append('Remove only the images and containers that hold them (as this account, no --force; '
                     f"{found['paths']}):")
        lines += ['  ' + command for command in commands]
    elif found['images'] or found['containers']:
        lines.append(f"Remove only these, as this account, no --force: containers {' '.join(found['containers']) or 'none'}; "
                     f"then images {' '.join(found['images']) or 'none'}. No command is printed: {found['paths']}. "
                     "Podman refuses any run root or tmp dir but the ones it recorded; run podman info as this "
                     "account: a mismatch error names the recorded value. Ask the owner before removing anything.")
    if found['unreferenced']:
        lines.append('No image or container holds layer(s) ' + ' '.join(found['unreferenced']) + ' (an interrupted '
                     'build); Podman cannot remove a bare layer. Ask the owner before resetting this account\'s store.')
    if found.get('malformed'):
        lines.append(f"{found['malformed']} record(s) holding them have IDs that are not 64 hex digits, so they are not "
                     'printed: this store\'s metadata was altered. Do not run anything from it; ask the owner.')
    lines.append('Build only with deploy/containers/build-image, which never uses this store.')
    return '\n'.join(lines)


class CheckFailed(ValueError):
    """A named qualification check failed; the owner's report shows which one, and `log` says why."""

    def __init__(self, check, detail, log=()):
        super().__init__(f'{check}: {detail}')
        self.check, self.log = check, list(log)


DEBUG_LINES = 30
CONTROL = re.compile('[\x00-\x08\x0b-\x1f\x7f-\x9f\u061c\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]')
ANSI = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]')


def clean(line):
    return CONTROL.sub('', ANSI.sub('', line)).strip()[:300]


def unshare(flag, paths, env):
    """This script's denial check inside `podman unshare`, from / (never the caller's directory: an owner console
    sits in /root, which the agent's namespace can't enter). A Podman-level failure, as opposed to the check's own
    "allowed" (exit 1), is rerun once with --log-level=debug; the result is still a failure."""
    command = ['/usr/bin/podman', 'unshare', '/usr/bin/python3', '-I', str(Path(__file__).resolve()), flag]
    result = subprocess.run(command, input=json.dumps(paths), capture_output=True, text=True, env=env, timeout=90, cwd='/')
    if result.returncode in (0, 1):
        return result, []
    log = ['podman: ' + clean(line) for line in (result.stderr or '').splitlines()[-10:] if clean(line)]
    try:
        debug = subprocess.run([command[0], '--log-level=debug', *command[1:]], input=json.dumps(paths),
                               capture_output=True, text=True, env=env, timeout=90, cwd='/')
        lines = [clean(line) for line in (debug.stderr or '').splitlines() if clean(line)]
        log += ['podman-debug: ' + line for line in lines[-DEBUG_LINES:]]
    except subprocess.TimeoutExpired:
        log.append('podman-debug: rerun timed out')
    return result, log


def alive(pid):
    try:
        os.stat(f'/proc/{pid}')
        return True
    except FileNotFoundError:
        return False


def last_line(text):
    lines = [line.strip() for line in (text or '').splitlines() if line.strip()]
    return lines[-1][:300] if lines else 'no output'


def denied(paths, *, read=False):
    for path in paths:
        try:
            flags = os.O_RDONLY if read else os.O_WRONLY | os.O_NOFOLLOW
            fd = os.open(path, flags | os.O_NONBLOCK)
        except OSError as error:
            if error.errno not in (errno.EACCES, errno.EPERM, errno.EROFS):
                return False
        else:
            os.close(fd)
            return False
    return bool(paths)


def podman_env(account):
    """Podman's environment for this probe. The kit runs `podman system migrate` with exactly this one afterwards
    (same XDG_RUNTIME_DIR, so the same pause.pid), which ends the pause process the probe left behind."""
    return {'HOME': account.pw_dir, 'XDG_RUNTIME_DIR': f'/run/user/{account.pw_uid}', 'PATH': '/usr/bin:/bin',
            'CONTAINERS_CONF': '/dev/null', 'CONTAINERS_CONF_OVERRIDE': '/dev/null',
            'CONTAINERS_STORAGE_CONF': '/srv/acc/containers/storage.conf'}


def probe(image, namespace_pid=None):
    if os.getuid() == 0:
        raise CheckFailed('agent_identity', 'run as the agent account, outside a container')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', image):
        raise CheckFailed('image_pin', 'expected a pinned image ID')
    env = podman_env(pwd.getpwuid(os.getuid()))
    if type(namespace_pid) is not int or namespace_pid <= 1:
        raise CheckFailed('namespace_service', 'owner must supply a live root-owned full-user-namespace service PID')
    gone = CheckFailed('namespace_service_exited', f'namespace service PID {namespace_pid} has exited (or never '
                       'started), so its boundary cannot be tested; check the acc-cq-* unit, not the host boundary')
    if not alive(namespace_pid):
        raise gone
    namespace_paths = [f'/proc/{namespace_pid}/{suffix}' for suffix in ('root', 'fd/0', 'ns/user', 'ns/mnt')]
    if not denied(namespace_paths, read=True):
        # A vanished process also fails the open (ENOENT): never report that as an accessible namespace.
        if not alive(namespace_pid):
            raise gone
        raise CheckFailed('namespace_access', 'host account can access the execution namespace')
    namespace_attempt, log = unshare('--attempt-read', namespace_paths, env)
    if namespace_attempt.returncode != 0 or namespace_attempt.stdout.strip() != 'denied':
        raise CheckFailed('namespace_unshare', 'unshare execution-namespace access denial not demonstrated '
                          f'(exit {namespace_attempt.returncode}: {last_line(namespace_attempt.stderr or namespace_attempt.stdout)})', log)
    name = 'acc-ct05-' + uuid.uuid4().hex
    rootfs = STORE / 'rootfs' / image[7:]
    for directory in (rootfs, *rootfs.parents):
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise CheckFailed('rootfs_ancestors', f'execution root or ancestor is unprotected: {directory}')
    found = shadowing(image, env['HOME'])
    if shadowed(found):
        raise CheckFailed('account_store', explain(image, found))
    root_fd = os.open(rootfs, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    # The launcher binds its held root (BindReadOnlyPaths=/proc/self/fd/N) into staging and passes that path.
    # The agent can't bind, and rootless Podman can't open another process's /proc/PID/fd from inside its user
    # namespace, so the probe passes the protected store path itself (root-owned ancestors: the agent can't swap
    # it) and proves identity against the held descriptor, before and after the run and through the container.
    source = str(rootfs)

    def same_root(info):
        held = os.fstat(root_fd)
        return (info.st_dev, info.st_ino) == (held.st_dev, held.st_ino)
    attempted = False

    def podman(*args):
        return subprocess.run(['/usr/bin/podman', *args], env=env, check=True,
                              capture_output=True, text=True, timeout=90, cwd='/').stdout

    try:
        pin = os.open('.acc-image-id', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd)
        try:
            info = os.fstat(pin)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022
                    or os.read(pin, 128) != (image + '\n').encode()):
                raise CheckFailed('held_rootfs_pin', 'execution root image pin mismatch')
        finally:
            os.close(pin)
        if not same_root(os.stat(source)):
            raise CheckFailed('held_rootfs_runtime', 'the store path no longer names the held protected root')
        attempted = True
        try:
            podman('run', '--pull=never', '--name=' + name, '--detach', '--read-only', '--network=none',
                   '--userns=keep-id:uid=1000,gid=1000', '--user=1000:1000', '--passwd=false',
                   '--rootfs', '--entrypoint=/bin/sleep', source, '120')
        except subprocess.CalledProcessError as error:
            raise CheckFailed('held_rootfs_run', f'podman run exited {error.returncode}: {last_line(error.stderr)}') from None
        info = json.loads(podman('container', 'inspect', name))[0]
        try:  # the agent owns the container's user namespace, so it may inspect its root from the host
            actual = os.stat(f'/proc/{int(info["State"]["Pid"])}/root')
        except OSError as error:
            raise CheckFailed('held_rootfs_identity', f'cannot inspect the container root from the host: {error}') from None
        if (info['Rootfs'] != source or not same_root(actual) or not same_root(os.stat(source))
                or not info['HostConfig']['ReadonlyRootfs']):
            raise CheckFailed('held_rootfs_runtime', 'runtime is not using the held protected root read-only')
        paths = []
        def walk_error(error):
            raise error

        for layer in (rootfs,):
            for root, dirs, files in os.walk(layer, followlinks=False, onerror=walk_error):
                for entry in [Path(root), *(Path(root) / n for n in dirs + files)]:
                    mode = entry.lstat()
                    if mode.st_uid != 0 or (not entry.is_symlink() and mode.st_mode & 0o022):
                        raise CheckFailed('image_entry_modes', f'image entry can be chmoded or replaced by an agent: {entry}')
                for filename in files:
                    path = Path(root) / filename
                    if path.is_file() and not path.is_symlink():
                        paths.append(str(path))
        if not denied(paths):
            raise CheckFailed('direct_write', 'host account can open an image file for writing')
        attempt, log = unshare('--attempt', paths, env)
        if attempt.returncode != 0 or attempt.stdout.strip() != 'denied':
            raise CheckFailed('unshare_write', 'unshare write denial not demonstrated '
                              f'(exit {attempt.returncode}: {last_line(attempt.stderr or attempt.stdout)})', log)
        return {'account': pwd.getpwuid(os.getuid()).pw_name, 'image': image, 'time': time.time(),
                'status': 'pass', 'direct_write': 'denied', 'unshare_write': 'denied',
                'runtime_layers': 'protected', 'execution_source': 'held-rootfs',
                'namespace_boundary': 'denied', 'files': len(paths)}
    finally:
        try:
            if attempted:
                podman('rm', '--force', '--ignore', name)
        finally:
            os.close(root_fd)


def main(argv):
    if argv in (['--attempt'], ['--attempt-read']):
        if not denied(json.load(sys.stdin), read=argv[0] == '--attempt-read'):
            print('allowed: an image or namespace path opened from inside podman unshare', file=sys.stderr)
            raise SystemExit(1)
        print('denied')
        return
    if argv[:1] == ['--check-store'] and len(argv) == 2:
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', argv[1]) or os.getuid() == 0:
            raise SystemExit('usage, as the agent account: image_store.py --check-store sha256:FULL_64_HEX_IMAGE_ID')
        found = shadowing(argv[1], pwd.getpwuid(os.getuid()).pw_dir)
        print(explain(argv[1], found) if shadowed(found)
              else 'No layer or ID of the pinned image is in this account\'s store.')
        raise SystemExit(1 if shadowed(found) else 0)
    try:
        print(json.dumps(probe(argv[0], int(argv[1]))))
    except CheckFailed as failure:
        # Podman's own lines first; the last line names the failed check.
        for line in failure.log:
            print(line, file=sys.stderr)
        print(f'image_store: FAILED {failure}', file=sys.stderr)
        raise SystemExit(1) from None
    except Exception as error:
        print(f'image_store: FAILED unexpected: {type(error).__name__}: {error}', file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main(sys.argv[1:])
