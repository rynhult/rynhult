"""Host-access guard contracts: these never inspect the named production paths."""
import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux live-root gate tripwire')


def guard(tmp_path):
    from host_isolation import HostGuard
    return HostGuard(read_roots=(tmp_path / 'checkout',), home='/home/real-owner')


@pytest.mark.parametrize('path', [
    '/srv/acc/containers/audit/events.jsonl', '/srv/acc/data/acc.db', '/srv/rynhult/data/db',
    '/etc/default/accd', '/etc/default/rynhult-node', '/etc/acc/broker/a.env', '/etc/acc-node.env',
    '/run/accd.sock', '/run/acc-broker/a.sock', '/var/run/accd.sock', '/var/lib/acc-node/state',
    '/etc/systemd/system/accd.service', '/root/.codex/auth.json', '/home/real-owner/.claude.json',
])
def test_production_paths_are_refused_before_access(tmp_path, path):
    from host_isolation import HostAccessError
    with pytest.raises(HostAccessError):
        guard(tmp_path).path(path)


@pytest.mark.parametrize('event,args', [
    ('open', ('/srv/acc/data/db', 'r', 0)),
    ('os.scandir', ('/srv/acc/containers/audit',)),
    ('sqlite3.connect', ('file:/srv/acc/data/acc.db?mode=ro',)),
    ('socket.connect', (None, '/run/acc-containers.sock')),
    ('socket.bind', (None, '/run/accd.sock')),
    ('socket.sendto', (None, '/run/acc-containers.sock')),
    ('os.rename', ('/tmp/synthetic', '/srv/acc/data/db', -1, -1)),
])
def test_native_io_audit_events_are_refused(tmp_path, event, args):
    from host_isolation import HostAccessError
    with pytest.raises(HostAccessError):
        guard(tmp_path).audit(event, args)


def test_checkout_reads_allowed_but_installation_siblings_refused(tmp_path):
    from host_isolation import HostAccessError, HostGuard
    checked = HostGuard(read_roots=('/srv/acc/workspaces/test-checkout',), home='/root')
    checked.path('/srv/acc/workspaces/test-checkout/tests/test.py')
    with pytest.raises(HostAccessError):
        checked.path('/srv/acc/workspaces/another-session/private')
    with pytest.raises(HostAccessError):
        checked.path('/srv/acc/workspaces/test-checkout/../../data/db')


def test_temporary_data_and_pure_path_values_are_allowed(tmp_path):
    checked = guard(tmp_path)
    checked.path(tmp_path / 'srv/acc/data/acc.db')
    assert str(Path('/srv/acc/data/acc.db')) == '/srv/acc/data/acc.db'
    checked.audit('open', (7, 'r', 0))  # already-open fd


@pytest.mark.parametrize('argv', [
    ['tmux', 'list-sessions'], ['tmux', '-L', 'acc', 'kill-server'],
    ['systemctl', 'stop', 'accd.service'], ['systemctl', '--user', 'restart', 'acc-broker.service'],
    ['systemd', '--system'], ['pkill', '-f', 'synthetic'], ['killall', 'synthetic'],
])
def test_real_host_control_commands_refused(tmp_path, argv):
    from host_isolation import HostAccessError
    with pytest.raises(HostAccessError):
        guard(tmp_path).command('/usr/bin/' + argv[0], argv, os.environ)


def test_own_tmux_and_offline_systemd_allowed(tmp_path):
    checked = guard(tmp_path)
    checked.command('/usr/bin/tmux', ['tmux', '-L', 'acc-test-synthetic', 'list-sessions'], {})
    checked.command('/usr/bin/systemctl', ['systemctl', '--root', str(tmp_path), 'enable', 'accd.socket'], {})


def test_shared_fixture_paths_are_disposable(env):
    root = env['home'].parent
    for field in ('data_dir', 'run_dir', 'root_run_dir', 'broker_socket', 'broker_env_dir', 'accounts_dir',
                  'updates_dir', 'packages_dir', 'containers_dir', 'agent_home', 'catalog_local_dir',
                  'workspaces_root', 'hoststate_path', 'git_dir'):
        assert getattr(env['settings'], field).is_relative_to(root), field
    for field in ('containers_socket', 'containers_attach_root'):
        assert getattr(env['broker'], field).is_relative_to(root), field


def test_caught_host_access_still_fails_the_test(tmp_path):
    """Exercise the real conftest plugin in a child pytest, without touching a live file."""
    import subprocess
    import sys

    from _pytest.pytester import RunResult

    case = tmp_path / 'test_tripwire.py'
    case.write_text('''import os, sqlite3, socket
from pathlib import Path
import pytest

@pytest.mark.parametrize("kind", ["open", "stat", "sqlite", "socket", "symlink", "relative", "write"])
def test_catches(kind, tmp_path):
    with pytest.raises(PermissionError):
        if kind == "open":
            Path("/srv/acc/containers/audit/synthetic").read_bytes()
        elif kind == "stat":
            os.stat("/etc/default/accd")
        elif kind == "sqlite":
            sqlite3.connect("file:/srv/acc/data/acc.db?mode=ro", uri=True)
        elif kind == "socket":
            with socket.socket(socket.AF_UNIX) as peer:
                peer.connect("/run/acc-containers.sock")
        elif kind == "symlink":
            link = tmp_path / "alias"
            link.symlink_to("/srv/acc/data")
            (link / "synthetic").read_bytes()
        elif kind == "relative":
            fd = os.open("/", os.O_RDONLY)
            try:
                os.open("srv/acc/data/synthetic", os.O_RDONLY, dir_fd=fd)
            finally:
                os.close(fd)
        else:
            Path("/srv/acc/data/synthetic").write_text("never written")
''')
    repo = Path(__file__).resolve().parents[1]
    process = subprocess.run([sys.executable, '-m', 'pytest', '-q', '--color=no', '-p', 'host_isolation', str(case)],
                             cwd=tmp_path, env={**os.environ, 'PYTHONPATH': f'{repo / "tests"}:{repo / "src"}',
                                                'PY_COLORS': '0', 'NO_COLOR': '1'},
                             capture_output=True, text=True, timeout=60)
    assert process.returncode == 1, process.stdout + process.stderr
    outcomes = RunResult.parse_summary_nouns(process.stdout.splitlines())
    assert outcomes.get('passed') == 7 and outcomes.get('errors') == 7, process.stdout + process.stderr
    assert 'test attempted host access' in process.stdout


def test_shared_api_world_does_not_sample_the_gate_host(env, monkeypatch):
    from acc import hostinfo
    from acc.server import host

    def forbidden(*args, **kwargs):
        pytest.fail('API fixture sampled the live gate host')
    monkeypatch.setattr(hostinfo, 'live', forbidden)
    assert host.live()['memory']['total'] > 0


@pytest.mark.parametrize('root', ['/root', '/home/real-owner'])
def test_home_policy_is_independent_of_gate_identity(tmp_path, root):
    from host_isolation import HostAccessError, HostGuard
    checked = HostGuard(home=root)
    with pytest.raises(HostAccessError):
        checked.path(root + '/.aws/credentials')


def test_podman_requires_all_disposable_stores_and_config(tmp_path):
    from host_isolation import HostAccessError
    checked = guard(tmp_path)
    env = {'HOME': str(tmp_path), 'XDG_CONFIG_HOME': str(tmp_path / 'config'),
           'XDG_DATA_HOME': str(tmp_path / 'data'), 'XDG_RUNTIME_DIR': str(tmp_path / 'runtime'),
           'CONTAINERS_CONF': '/dev/null',
           'CONTAINERS_CONF_OVERRIDE': '/dev/null', 'CONTAINERS_STORAGE_CONF': str(tmp_path / 'storage.conf')}
    argv = ['podman', '--root', str(tmp_path / 'store'), '--runroot', str(tmp_path / 'run'),
            '--tmpdir', str(tmp_path / 'tmp'), 'info']
    checked.command('/usr/bin/podman', argv, env)
    for unsafe, environment in [(['podman', 'info'], env), (argv, {'HOME': '/root'}),
                                (argv + ['--connection=production'], env),
                                (argv[:2] + ['/var/lib/containers'] + argv[3:], env)]:
        with pytest.raises(HostAccessError):
            checked.command('/usr/bin/podman', unsafe, environment)


def test_postgres_probe_uses_disposable_store(tmp_path_factory, monkeypatch):
    import inspect
    import subprocess

    import postgres_matrix

    calls = []
    monkeypatch.setattr(postgres_matrix.shutil, 'which', lambda name: '/usr/bin/podman' if name == 'podman' else None)

    def unavailable(argv, **kwargs):
        calls.append((argv, kwargs['env'] if 'env' in kwargs else {}))
        return subprocess.CompletedProcess(argv, 1, b'', b'disposable runtime unavailable')
    monkeypatch.setattr(postgres_matrix.subprocess, 'run', unavailable)
    fixture = postgres_matrix.postgres_url.__wrapped__
    args = {'tmp_path_factory': tmp_path_factory} if 'tmp_path_factory' in inspect.signature(fixture).parameters else {}
    with pytest.raises(pytest.skip.Exception, match='No usable throwaway'):
        next(fixture(**args))
    assert calls
    for argv, env in calls:
        for option in ('--root', '--runroot', '--tmpdir'):
            assert option in argv
            assert Path(argv[argv.index(option) + 1]).is_relative_to(tmp_path_factory.getbasetemp())
        for key in ('HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME'):
            assert Path(env[key]).is_relative_to(tmp_path_factory.getbasetemp())


def test_directory_relative_mutations_are_checked_without_executing_them(tmp_path):
    from host_isolation import HostAccessError
    checked = guard(tmp_path)
    root_fd = os.open('/', os.O_RDONLY)
    try:
        for event, args in [('os.remove', ('srv/acc/data/synthetic', root_fd)),
                            ('os.rename', ('tmp/synthetic', 'srv/acc/data/synthetic', root_fd, root_fd))]:
            with pytest.raises(HostAccessError):
                checked.audit(event, args)
    finally:
        os.close(root_fd)


def test_offline_systemctl_cannot_follow_temporary_alias_to_host_root(tmp_path):
    from host_isolation import HostAccessError
    alias = tmp_path / 'offline-root'
    alias.symlink_to('/')
    with pytest.raises(HostAccessError):
        guard(tmp_path).command('/usr/bin/systemctl', ['systemctl', '--root', str(alias), 'enable', 'accd'], {})


def test_live_editable_install_is_not_an_import_fallback():
    import sys
    repo = Path(__file__).resolve().parents[1]
    for entry in sys.path:
        value = os.path.abspath(entry)
        assert not value.startswith('/srv/acc/app/src'), entry
    assert os.environ['PYTHONPATH'].split(os.pathsep)[0] == str(repo / 'src')


@pytest.mark.parametrize('relative', ['deploy/acc-install', 'deploy/node/install.sh', 'deploy/setup.sh'])
def test_native_installer_subject_pins_state_reads_and_keeps_child_in_scratch(relative):
    from isolated_installers import REPO, dry_run_subject
    source = REPO / relative
    with dry_run_subject(['bash', str(source), '--dry-run'], {}) as (argv, _):
        staged = Path(argv[1])
        assert staged != source and staged.is_relative_to('/tmp')
        # The wizard and its child both use test state, not the live installation's enrollment/configuration.
        for script in staged.parent.iterdir():
            text = script.read_text()
            assert 'BROKER_ENV=/etc/acc/broker' not in text
            for path in ('/etc/acc-node.env', '/etc/default/accd', '/etc/systemd/system/accd.service'):
                assert path not in text
        if source.name == 'acc-install':
            for child in ('deploy/node/install.sh', 'deploy/setup.sh'):
                assert f'"$REPO/{child}"' not in staged.read_text()
    assert not staged.exists()


def test_collection_cannot_swallow_a_forbidden_read(tmp_path):
    import subprocess
    import sys

    case = tmp_path / 'test_collection_probe.py'
    case.write_text('''from pathlib import Path
try:
    Path('/srv/acc/containers/audit/synthetic').read_bytes()
except PermissionError:
    pass

def test_should_not_run():
    assert False, 'the collection guard did not stop this module'
''')
    repo = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, '-m', 'pytest', '-q', '-p', 'host_isolation', str(case)],
                            cwd=tmp_path, env={**os.environ, 'PYTHONPATH': f'{repo / "tests"}:{repo / "src"}'},
                            capture_output=True, text=True, timeout=60)
    assert result.returncode != 0
    assert 'host access during collection' in result.stdout + result.stderr
    assert 'the collection guard did not stop' not in result.stdout


def test_complete_factories_have_no_installed_path_defaults(tmp_path):
    from dataclasses import fields

    from isolated_settings import REPO, isolated_broker, isolated_settings
    from test_orchestrator_gates import config as gate_config
    from test_team_git_hardening import config as team_config

    for config in (isolated_settings(tmp_path), isolated_broker(tmp_path), gate_config(tmp_path), team_config(tmp_path)):
        for field in fields(config):
            value = getattr(config, field.name)
            paths = (value,) if isinstance(value, Path) else value if isinstance(value, tuple) else ()
            for path in paths:
                if isinstance(path, Path):
                    assert path.is_relative_to(tmp_path) or path.is_relative_to(REPO), (field.name, path)


def test_permission_probe_may_drop_to_nobody_but_not_elevate(tmp_path):
    import sys

    from host_isolation import HostAccessError
    checked = guard(tmp_path)
    checked.command('/usr/sbin/runuser', ['runuser', '-u', 'nobody', '--', sys.executable, '-c',
                                        'import os; assert os.geteuid() != 0'], {})
    with pytest.raises(HostAccessError):
        checked.command('/usr/sbin/runuser', ['runuser', '-u', 'root', '--', sys.executable], {})


def test_systemd_runtime_reads_are_distinct_from_installed_unit_directories(tmp_path):
    from host_isolation import HostAccessError

    checked = guard(tmp_path)
    # Classify strings only: no system file is opened for this policy test.
    checked.path('/etc/os-release')
    checked.path('/usr/lib/systemd/systemd')
    checked.path('/lib/systemd/systemd')
    for path in ('/etc/systemd/system', '/usr/lib/systemd/system', '/usr/lib/systemd/user',
                 '/etc/systemd/system/accd.service', '/usr/lib/systemd/system/rynhultd.service'):
        with pytest.raises(HostAccessError):
            checked.path(path)
    for path in ('/usr/lib/systemd/systemd', '/usr/lib/systemd', '/etc/systemd'):
        with pytest.raises(HostAccessError):
            checked.path(path, write=True)


def test_unlinking_an_inert_host_symlink_does_not_access_its_target(tmp_path):
    alias = tmp_path / 'inert-unit-link'
    alias.symlink_to('/etc/systemd/system/accd.service')
    alias.unlink()
    assert not os.path.lexists(alias)


def test_renaming_an_inert_host_symlink_does_not_access_its_target(tmp_path):
    alias, moved = tmp_path / 'unit-link', tmp_path / 'moved-link'
    alias.symlink_to('/etc/systemd/system/accd.service')
    alias.rename(moved)
    assert moved.is_symlink()
    moved.unlink()


def test_directory_entry_operations_still_refuse_linked_parents(tmp_path):
    from host_isolation import HostAccessError, HostGuard

    synthetic_host = tmp_path / 'synthetic-host'
    synthetic_host.mkdir()
    sentinel = synthetic_host / 'sentinel'
    sentinel.write_text('preserve')
    alias = tmp_path / 'linked-parent'
    alias.symlink_to(synthetic_host, target_is_directory=True)
    checked = HostGuard(home=synthetic_host)
    for event, args in [('os.remove', (str(alias / 'sentinel'), -1)),
                        ('os.rename', (str(alias / 'sentinel'), str(tmp_path / 'moved'), -1, -1))]:
        with pytest.raises(HostAccessError):
            checked.audit(event, args)
    assert sentinel.read_text() == 'preserve'
