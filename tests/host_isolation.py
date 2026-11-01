"""Test-only tripwire, not an OS sandbox. Never inspect production state to decide whether it exists."""
from __future__ import annotations

import os
import re
import shlex
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import unquote

import pytest


class HostAccessError(PermissionError):
    pass


class HostGuard:
    def __init__(self, *, read_roots=(), write_roots=(), home='/root', disposable_root_vm=False):
        self.read_roots = tuple(os.path.abspath(p) for p in read_roots)
        self.write_roots = tuple(os.path.abspath(p) for p in write_roots)
        self.home = os.path.abspath(home)
        self.violations = []
        self.enabled = True
        self.local = threading.local()
        self.disposable_root_vm = disposable_root_vm is True
        # Creation argv authorized in this worker only. Cleanup/membership cannot
        # target pre-existing fixture-looking names from another invocation.
        self._fixture_users = set()
        self._fixture_groups = set()

    def refuse(self, description):
        message = f'test attempted host access: {description}'
        self.violations.append(message)
        raise HostAccessError(message)

    def path(self, value, *, write=False):
        if value is None or isinstance(value, int):
            return
        value = os.fsdecode(value)
        if value.startswith('file:'):
            value = unquote(value[5:].split('?', 1)[0])
        path = "/" + os.path.abspath(value).lstrip("/")
        parts = path.split('/')
        if len(parts) >= 4 and parts[1] == 'tmp' and parts[2].startswith('tmux-') and parts[3] in ('acc', 'rynhult', 'default'):
            self.refuse(path)
        roots = self.write_roots if write else self.read_roots
        if any(path == root or path.startswith(root + '/') for root in roots):
            return
        if (path.startswith(('/srv/acc', '/srv/rynhult', '/opt/acc', '/opt/rynhult',
                             '/etc/acc', '/etc/rynhult', '/etc/default/acc', '/etc/default/rynhult',
                             '/run/acc', '/run/rynhult', '/var/run/acc', '/var/run/rynhult',
                             '/var/lib/acc', '/var/lib/rynhult', '/var/lib/containers', '/run/containers'))
                or path == '/root' or path.startswith('/root/')
                or path == '/home' or path.startswith('/home/')
                or path == self.home or path.startswith(self.home + '/')
                or any(path == unit or path.startswith(unit + '/') for unit in (
                    '/etc/systemd/system', '/etc/systemd/user', '/usr/lib/systemd/system',
                    '/usr/lib/systemd/user', '/lib/systemd/system', '/lib/systemd/user'))
                or write and any(path == root or path.startswith(root + '/') for root in (
                    '/etc/systemd', '/usr/lib/systemd', '/lib/systemd'))):
            self.refuse(path)

    def resolved_path(self, value, *, write=False):
        self.path(value, write=write)
        if value is None or isinstance(value, int) or getattr(self.local, 'resolving', False):
            return
        text = os.fsdecode(value)
        if text.startswith('file:'):
            text = unquote(text[5:].split('?', 1)[0])
        self.local.resolving = True
        try:
            # Patched lstat/readlink check each intermediate path before reading it.
            self.path(os.path.realpath(text), write=write)
        finally:
            self.local.resolving = False

    def disposable_folder_command(self, executable, argv):
        """Exact test-identity lifecycle, not general root command permission.

        Only the captured pytest opt-in counts; a runtime environment flag does
        not. Names/IDs match the disposable folder fixture, never real accounts.
        This classifier does not execute a command or inspect host identities.
        """
        if not self.disposable_root_vm or getattr(os, 'geteuid', lambda: None)() != 0:
            return False
        if (not isinstance(argv, (list, tuple)) or not argv or not all(isinstance(a, str) for a in argv)
                or argv[0] != executable):
            return False
        tool = Path(executable).name
        if executable not in ({'/usr/sbin/' + name for name in
                ('useradd', 'userdel', 'groupadd', 'groupdel', 'gpasswd', 'usermod')} | {'/usr/bin/gpasswd'}):
            return False
        if tool == 'useradd' and len(argv) == 11:
            if (argv[1:4] == ['--no-create-home', '--no-user-group', '--uid']
                    and re.fullmatch('61[0-9]{3}', argv[4])
                    and argv[5:10] == ['--gid', '65534', '--shell', '/usr/sbin/nologin', '--']
                    and re.fullmatch('accq[ha][0-9a-f]{8}', argv[10])):
                self._fixture_users.add(argv[10])
                return True
        elif tool == 'groupadd' and len(argv) == 5:
            if (argv[1] == '--gid' and re.fullmatch('6[0-4][0-9]{3}', argv[2]) and argv[3] == '--'
                    and re.fullmatch('ryn-repo-[0-9a-f]{16}', argv[4])):
                self._fixture_groups.add(argv[4])
                return True
        elif tool == 'userdel':
            return len(argv) == 3 and argv[1] == '--' and argv[2] in self._fixture_users
        elif tool == 'groupdel':
            return len(argv) == 3 and argv[1] == '--' and argv[2] in self._fixture_groups
        elif tool == 'usermod':
            return (len(argv) == 6 and argv[1:3] == ['--append', '--groups'] and argv[4] == '--'
                    and argv[3] in self._fixture_groups and argv[5] in self._fixture_users)
        elif tool == 'gpasswd':
            return (len(argv) == 4 and argv[1] == '--delete'
                    and argv[2] in self._fixture_users and argv[3] in self._fixture_groups)
        return False

    def command(self, executable, argv, env):
        if self.disposable_folder_command(os.fsdecode(executable), argv):
            return
        if isinstance(argv, str):
            argv = shlex.split(argv)
        argv = [os.fsdecode(a) for a in argv]
        executable = os.fsdecode(executable)
        if '/' not in executable:
            executable = shutil.which(executable, path=(env or os.environ).get('PATH')) or executable
        requested = Path(executable).name
        executable = os.path.realpath(executable)
        names = {requested, Path(executable).name, Path(argv[0]).name if argv else requested}
        if 'tmux' in names:
            if '-L' in argv and argv.index('-L') + 1 < len(argv):
                if argv[argv.index('-L') + 1].startswith('acc-test-'):
                    return
            if '-S' in argv and argv.index('-S') + 1 < len(argv):
                self.resolved_path(argv[argv.index('-S') + 1], write=True)
                if os.path.realpath(argv[argv.index('-S') + 1]).startswith('/tmp/'):
                    return
            self.refuse('tmux must use an owned acc-test-* socket or a temporary -S path')
        if 'systemctl' in names and not os.path.abspath(executable).startswith('/tmp/'):
            root = next((a.split('=', 1)[1] for a in argv if a.startswith('--root=')), None)
            if '--root' in argv and argv.index('--root') + 1 < len(argv):
                root = argv[argv.index('--root') + 1]
            if root:
                self.resolved_path(root, write=True)
                if os.path.realpath(root).startswith('/tmp/'):
                    return
            self.refuse('systemctl requires an offline temporary --root (or a fake executable)')
        if 'podman' in names and not os.path.abspath(executable).startswith('/tmp/'):
            # Only a disposable store/runroot/config may use the real container runtime.
            for option in ('--root', '--runroot', '--tmpdir'):
                if argv.count(option) != 1 or argv.index(option) + 1 >= len(argv):
                    self.refuse('Podman requires explicit disposable root/runroot/tmpdir')
                folder = argv[argv.index(option) + 1]
                if not os.path.abspath(folder).startswith('/tmp/'):
                    self.refuse('Podman storage must be temporary')
                self.resolved_path(folder, write=True)
                if not os.path.realpath(folder).startswith('/tmp/'):
                    self.refuse('Podman storage must be temporary')
            if any(a.startswith(('--remote', '--url', '--connection', '--root=', '--runroot=', '--tmpdir=')) for a in argv):
                self.refuse('Podman storage/connection overrides are not allowed')
            for key in ('HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_RUNTIME_DIR'):
                if not env or not os.path.abspath(env.get(key, '/')).startswith('/tmp/'):
                    self.refuse('Podman configuration must be temporary')
                self.resolved_path(env[key], write=True)
                if not os.path.realpath(env[key]).startswith('/tmp/'):
                    self.refuse('Podman configuration must be temporary')
            if env.get('CONTAINERS_CONF') != '/dev/null' or env.get('CONTAINERS_CONF_OVERRIDE') != '/dev/null':
                self.refuse('Podman must not inherit host containers.conf')
            storage = env.get('CONTAINERS_STORAGE_CONF', '')
            if not storage or not os.path.abspath(storage).startswith('/tmp/'):
                self.refuse('Podman requires disposable storage configuration')
            self.resolved_path(storage)
            if not os.path.realpath(storage).startswith('/tmp/'):
                self.refuse('Podman requires disposable storage configuration')
            return
        if any(name.startswith(('acc-', 'rynhult')) or name == 'acc' for name in names) and not (
                os.path.abspath(executable).startswith(('/tmp/', str(Path(__file__).resolve().parents[1]) + '/'))):
            self.refuse('installed agent tool; use this checkout with disposable fixtures')
        if 'runuser' in names and argv[1:4] == ['-u', 'nobody', '--']:
            return  # Existing permission regressions deliberately drop privilege; never create an account.
        if names.intersection(('sudo', 'su', 'runuser', 'systemd', 'systemd-run', 'loginctl', 'useradd', 'usermod',
                    'userdel', 'groupadd', 'groupdel', 'gpasswd', 'pkill', 'killall', 'docker', 'claude', 'codex',
                    'copilot', 'cursor', 'opencode', 'antigravity')) and not os.path.abspath(executable).startswith('/tmp/'):
            self.refuse('real host control or provider command; use a fake executable')

    def at_path(self, path, directory=-1, *, write=False, follow_leaf=True):
        if not isinstance(path, int) and path is not None and not os.path.isabs(path) and directory not in (-1, None):
            path = os.path.join(os.readlink(f'/proc/self/fd/{directory}'), os.fsdecode(path))
        if follow_leaf or path is None or isinstance(path, int):
            self.resolved_path(path, write=write)
        else:
            # unlink/rename act on the directory entry, not a symlink's target. Still guard the entry's lexical
            # name and fully resolve its parent: a link in an ancestor can redirect the real operation.
            self.path(path, write=write)
            self.resolved_path(os.path.dirname(os.path.abspath(path)), write=write)

    def audit(self, event, args):
        if not self.enabled:
            return
        if event == 'open':
            flags = args[2] or 0
            self.resolved_path(args[0], write=bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)))
        elif event in ('os.listdir', 'os.scandir', 'os.chdir', 'sqlite3.connect'):
            self.resolved_path(args[0], write=event == 'sqlite3.connect')
        elif event in ('os.remove', 'os.rmdir'):
            self.at_path(args[0], args[1], write=True, follow_leaf=False)
        elif event in ('os.mkdir', 'os.chmod', 'os.chown', 'os.truncate', 'os.utime'):
            self.at_path(args[0], args[-1] if event != 'os.truncate' else -1, write=True)
        elif event in ('os.rename', 'os.link'):
            self.at_path(args[0], args[2], write=True, follow_leaf=event == 'os.link')
            self.at_path(args[1], args[3], write=True, follow_leaf=False)
        elif event == 'os.symlink':
            self.at_path(args[1], args[2], write=True, follow_leaf=False)  # inert hostile targets are legitimate test data
        elif event in ('socket.connect', 'socket.bind', 'socket.sendto') and isinstance(args[1], (str, bytes)):
            self.resolved_path(args[1], write=True)
        elif event == 'os.system':
            self.refuse('os.system is not isolated; use an explicit test subprocess')
        elif event in ('os.exec', 'os.posix_spawn'):
            self.resolved_path(args[0])
            self.command(args[0], args[1], args[2])
        elif event == 'subprocess.Popen':
            executable, argv, cwd, env = args
            self.resolved_path(executable)
            self.resolved_path(cwd)
            self.command(executable, argv, env)


_GUARD = None
_ORIGINALS = {}


def pytest_addoption(parser):
    parser.addoption('--disposable-root-vm', action='store_true', default=False,
        help='Explicitly opt into root qualification ONLY inside a disposable VM; host isolation stays enabled.')


def disposable_root_skip_reason(config):
    missing = []
    euid = getattr(os, 'geteuid', lambda: None)()
    if euid != 0:
        missing.append(f'requires EUID 0 (current EUID: {euid})')
    if config.getoption('disposable_root_vm') is not True:
        missing.append('requires explicit opt-in: --disposable-root-vm or ACC_FOLDER_QUALIFICATION_DISPOSABLE=1 at pytest startup')
    return '; '.join(missing) + '. Never use the live/shared host.' if missing else None


@pytest.fixture
def disposable_root_vm(request):
    reason = disposable_root_skip_reason(request.config)
    if reason:
        pytest.skip(reason)


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node):
    # xdist workers start after the controller has scrubbed its environment.
    # Send one boolean in pytest's private control record, never host credentials.
    node.workerinput['acc_disposable_root_vm'] = node.config.getoption('disposable_root_vm') is True


def pytest_configure(config):
    global _GUARD
    # Capture before configure_test_environment strips ALL ACC_/RYNHULT_ values.
    # Per-test HOME rebuilding and child processes must not inherit the opt-in.
    config.option.disposable_root_vm = (
        config.getoption('disposable_root_vm') is True
        or os.environ.get('ACC_FOLDER_QUALIFICATION_DISPOSABLE') == '1'
        or getattr(config, 'workerinput', {}).get('acc_disposable_root_vm') is True)
    configure_test_environment(config)
    sys.dont_write_bytecode = True
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    import pwd
    repo = Path(__file__).resolve().parents[1]
    _GUARD = HostGuard(read_roots=(repo, sys.prefix, '/srv/acc/tools/ms-playwright'),
                       write_roots=(repo,), home=pwd.getpwuid(os.getuid()).pw_dir,
                       disposable_root_vm=config.getoption('disposable_root_vm'))
    # The shared venv's editable install adds the live checkout to sys.path. Never probe it
    # as a fallback while resolving optional imports in this test checkout.
    safe_imports = []
    for entry in sys.path:
        try:
            _GUARD.path(entry)
        except HostAccessError:
            continue
        safe_imports.append(entry)
    sys.path[:] = safe_imports
    _GUARD.violations.clear()  # classification only: no filesystem operation occurred above
    os.environ['PYTHONPATH'] = str(repo / 'src')
    sys.addaudithook(_GUARD.audit)
    ancestors = {str(p) for root in (repo, Path(sys.prefix)) for p in root.parents}
    for name in ('stat', 'lstat', 'access', 'readlink', 'open'):
        original = getattr(os, name)
        _ORIGINALS[name] = original

        def checked(path, *args, _original=original, _name=name, **kwargs):
            # pytest/importlib inspect checkout ancestors while discovering conftests.
            # Permit only their metadata, never listing or opening siblings.
            ancestor = (not isinstance(path, int) and _name in ("stat", "lstat", "readlink")
                        and os.path.abspath(path) in ancestors)
            if _GUARD.enabled and not ancestor:
                target = path
                if not isinstance(path, int) and not os.path.isabs(path) and kwargs.get('dir_fd') is not None:
                    parent = _ORIGINALS['readlink'](f"/proc/self/fd/{kwargs['dir_fd']}")
                    target = os.path.join(parent, os.fsdecode(path))
                flags = args[0] if _name == 'open' and args else 0
                if _name in ('readlink', 'lstat') or kwargs.get('follow_symlinks') is False:
                    _GUARD.path(target)
                    parent = os.path.dirname(os.path.abspath(target))
                    if parent not in ancestors:
                        _GUARD.resolved_path(parent)
                else:
                    _GUARD.resolved_path(target, write=bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)))
            return _original(path, *args, **kwargs)
        setattr(os, name, checked)


@pytest.hookimpl(wrapper=True, trylast=True)
def pytest_runtest_teardown(item, nextitem):
    try:
        return (yield)
    finally:
        if _GUARD.violations:
            messages = list(dict.fromkeys(_GUARD.violations))
            _GUARD.violations.clear()
            pytest.fail('\n'.join(messages), pytrace=False)


@pytest.hookimpl(tryfirst=True)
def pytest_collection_finish(session):
    if _GUARD.violations:
        raise pytest.UsageError('host access during collection: ' + '\n'.join(dict.fromkeys(_GUARD.violations)))


def pytest_sessionfinish(session, exitstatus):
    # A late worker must not lose its violation between tests or after the final teardown.
    if _GUARD.violations:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        session.shouldfail = 'late host-access violation'
        terminal = session.config.pluginmanager.get_plugin('terminalreporter')
        if terminal:
            terminal.write_line('late host-access violation: ' + '\n'.join(dict.fromkeys(_GUARD.violations)))


def pytest_unconfigure(config):
    if _GUARD is not None:
        _GUARD.enabled = False
    for name, original in _ORIGINALS.items():
        setattr(os, name, original)


def configure_test_environment(config):
    # tempfile may have cached an inherited deployment TMPDIR before pytest configured itself.
    tempfile.tempdir = '/tmp'
    for key in ('TMPDIR', 'TMP', 'TEMP'):
        os.environ[key] = '/tmp'
    for key in list(os.environ):
        if key.startswith(('AWS_', 'AZURE_', 'GOOGLE_', 'CLOUDSDK_', 'CLOUDFLARE_', 'DIGITALOCEAN_',
                           'HETZNER_', 'ANTHROPIC_', 'OPENAI_', 'CLAUDE_', 'CODEX_', 'COPILOT_')) or key in (
                'GH_TOKEN', 'GITHUB_TOKEN', 'NPM_TOKEN', 'NODE_AUTH_TOKEN', 'BASH_ENV', 'ENV', 'ZDOTDIR',
                'SSH_AUTH_SOCK', 'DBUS_SESSION_BUS_ADDRESS', 'DBUS_SYSTEM_BUS_ADDRESS', 'TMUX', 'TMUX_TMPDIR',
                'DOCKER_HOST', 'CONTAINER_HOST', 'GIT_SSH', 'GIT_SSH_COMMAND', 'GIT_PROXY_COMMAND', 'GIT_ASKPASS',
                'SSH_ASKPASS', 'SSH_ASKPASS_REQUIRE', 'CONTAINERS_CONF', 'CONTAINERS_CONF_OVERRIDE',
                'CONTAINERS_STORAGE_CONF', 'CONTAINER_CONNECTION', 'REGISTRY_AUTH_FILE', 'DOCKER_AUTH_CONFIG') or key.startswith('GIT_CONFIG'):
            os.environ.pop(key, None)
    os.environ['GIT_CONFIG_NOSYSTEM'] = '1'
    os.environ['GIT_TERMINAL_PROMPT'] = '0'
    os.environ['PATH'] = os.pathsep.join((str(Path(sys.prefix) / 'bin'), '/usr/local/bin', '/usr/bin', '/bin', '/usr/sbin', '/sbin'))
    session_home = tempfile.TemporaryDirectory(prefix='acc-pytest-home-')
    config.add_cleanup(session_home.cleanup)
    home = Path(session_home.name) / 'home'
    home.mkdir(mode=0o700)
    (home.parent / 'xdg-run').mkdir(mode=0o700)
    os.environ.update(home_environment(home))
    # A real agent session exports both spellings. RYNHULT_ wins over ACC_,
    # so fixture-only ACC_RUN_DIR overrides otherwise still target its spool.
    # Never let test subprocesses inherit the live session's hook/guard targets.
    from acc.env import PROTECTED_SESSION_VARIABLES
    for key in set(os.environ) | PROTECTED_SESSION_VARIABLES:
        if key in PROTECTED_SESSION_VARIABLES or key.startswith(("ACC_", "RYNHULT_")):
            os.environ.pop(key, None)
    os.environ['RYNHULT_TEST'] = '1'
    os.environ['ACC_TEST'] = '1'  # No real telemetry transport in any test process.
    if config.option.basetemp is None:
        # Do not participate in other pytest processes' numbered-directory retention/cleanup.
        root = tempfile.TemporaryDirectory(prefix="acc-pytest-")
        config.option.basetemp = str(Path(root.name) / "tmp")
        config.add_cleanup(root.cleanup)


@pytest.fixture(autouse=True)
def isolated_process_home(tmp_path_factory, monkeypatch):
    """No test or child process inherits the gate runner's provider configuration."""
    tmp_path = tmp_path_factory.mktemp('host-isolation')
    from acc import backup_receiver_check, backup_targets
    from acc.server.config import Settings
    binding = tmp_path / "backup-target.json"
    monkeypatch.setattr(backup_targets, "BINDING", binding)
    monkeypatch.setattr(backup_receiver_check, "BINDING", binding)
    monkeypatch.setattr(backup_receiver_check, "DATA", tmp_path / "receiver")
    monkeypatch.setenv("ACC_UPDATE_ROOT", str(tmp_path / "updates"))
    monkeypatch.setenv("ACC_PACKAGES_ROOT", str(tmp_path / "packages"))

    home = tmp_path / "home"
    home.mkdir(mode=0o700, exist_ok=True)
    (home.parent / "xdg-run").mkdir(mode=0o700, exist_ok=True)
    for key, value in home_environment(home).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PATH", os.pathsep.join((str(Path(sys.prefix) / 'bin'),
                                               '/usr/local/bin', '/usr/bin', '/bin', '/usr/sbin', '/sbin')))
    for key in ('BASH_ENV', 'ENV', 'ZDOTDIR', 'DBUS_SESSION_BUS_ADDRESS', 'GIT_CONFIG', 'GIT_CONFIG_GLOBAL',
                'GIT_CONFIG_SYSTEM', 'GIT_CONFIG_COUNT', 'SSH_AUTH_SOCK', 'DOCKER_HOST', 'CONTAINER_HOST'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_TMPDIR", raising=False)
    monkeypatch.setattr(Settings, "account_home", staticmethod(lambda account: tmp_path / "homes" / account))


def home_environment(home):
    folders = {
        "XDG_CONFIG_HOME": home / ".config", "XDG_DATA_HOME": home / ".local/share",
        "XDG_CACHE_HOME": home / ".cache", "XDG_STATE_HOME": home / ".local/state",
        "XDG_RUNTIME_DIR": home.parent / "xdg-run", "CODEX_HOME": home / ".codex",
        "CLAUDE_CONFIG_DIR": home / ".claude", "COPILOT_HOME": home / ".copilot",
        "CURSOR_CONFIG_DIR": home / ".cursor", "GNUPGHOME": home / ".gnupg",
        "DOCKER_CONFIG": home / ".docker", "KUBECONFIG": home / ".kube/config",
        "GH_CONFIG_DIR": home / ".config/gh", "AZURE_CONFIG_DIR": home / ".azure",
        "CLOUDSDK_CONFIG": home / ".config/gcloud", "AWS_SHARED_CREDENTIALS_FILE": home / ".aws/credentials",
        "AWS_CONFIG_FILE": home / ".aws/config", "NPM_CONFIG_USERCONFIG": home / ".npmrc",
    }
    return {'HOME': str(home), **{key: str(value) for key, value in folders.items()}}
