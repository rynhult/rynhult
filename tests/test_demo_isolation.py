"""Demo and e2e must never inherit live host control directories."""
import inspect
import os
import runpy
import sys
import uuid
from pathlib import Path

import pytest

from acc.server.config import Settings


@pytest.fixture(autouse=True)
def restore_device_helpers(monkeypatch):
    # The standalone browser harness replaces these only within its own process.
    # These isolation tests call main in-process and must restore both functions.
    from acc.server import devices, remote_access
    monkeypatch.setattr(devices, 'sign', devices.sign)
    monkeypatch.setattr(remote_access, 'request', remote_access.request)


@pytest.mark.parametrize('script', ['scripts/demo_instance.py', 'tests/e2e_server.py'])
async def test_harness_runtime_paths_stay_in_its_state_directory(tmp_path, monkeypatch, script):
    repo = Path(__file__).resolve().parents[1]
    # main sets fake CLI HOME and git identity; restore those at test teardown.
    for key in ('HOME', 'FAKE_HOOK', 'GIT_AUTHOR_NAME', 'GIT_AUTHOR_EMAIL', 'GIT_COMMITTER_NAME', 'GIT_COMMITTER_EMAIL'):
        monkeypatch.setenv(key, os.environ.get(key, ''))
    monkeypatch.setattr(sys, 'path', list(sys.path))
    from acc.server import host
    monkeypatch.setattr(host, 'live', host.live)  # the demo replaces it; put the real one back afterwards
    namespace = runpy.run_path(str(repo / script))
    main = namespace['main']
    captured = {}
    broker_type = main.__globals__['BrokerConfig']

    def broker_config(*args, **kwargs):
        captured['broker'] = broker_type(*args, **kwargs)
        return captured['broker']

    class ConfigCaptured(Exception):
        pass

    def settings(*args, **kwargs):
        captured['settings'] = Settings(*args, **kwargs)
        raise ConfigCaptured

    monkeypatch.setitem(main.__globals__, 'BrokerConfig', broker_config)
    monkeypatch.setitem(main.__globals__, 'Settings', settings)
    state = tmp_path / 'demo'
    with pytest.raises(ConfigCaptured):
        # The e2e server also takes its owned tmux socket name; configuration is captured before any server starts.
        extra = (f'acc-test-isolation-{uuid.uuid4().hex[:8]}',) if 'socket_name' in inspect.signature(main).parameters else ()
        await main(state, 18761, *extra)
    config = captured['settings']
    for field in ('data_dir', 'run_dir', 'root_run_dir', 'broker_socket', 'broker_env_dir',
                  'accounts_dir', 'updates_dir', 'packages_dir', 'publish_dir', 'catalog_local_dir', 'containers_dir',
                  'workspaces_root', 'hoststate_path', 'git_dir', 'agent_home'):
        assert getattr(config, field).is_relative_to(state), (script, field, getattr(config, field))
    for field in ('containers_socket', 'containers_attach_root'):
        assert getattr(captured['broker'], field).is_relative_to(state), (script, field)
    assert captured['broker'].catalog_dir == repo / 'catalog'
    if script == 'scripts/demo_instance.py':  # the demo's screenshots show its hook path: never this checkout's (R12)
        assert Path(captured['broker'].hook_path).is_relative_to(state)
        assert Path(config.hook_path).is_relative_to(state) and Path(config.statusline_path).is_relative_to(state)
        assert Path(captured['broker'].hook_path).with_name('acc-guard').is_file()
    else:
        assert captured['broker'].hook_path == str(repo / 'bin/acc-hook')


@pytest.mark.parametrize('script', ['scripts/demo_instance.py', 'tests/e2e_server.py'])
async def test_harness_serves_fixed_host_metrics(tmp_path, monkeypatch, script):
    """Wiki screenshots show the harness's live CPU, memory and load, never the machine that runs it."""
    from acc.server import host
    repo = Path(__file__).resolve().parents[1]
    for key in ('HOME', 'FAKE_HOOK', 'GIT_AUTHOR_NAME', 'GIT_AUTHOR_EMAIL', 'GIT_COMMITTER_NAME', 'GIT_COMMITTER_EMAIL'):
        monkeypatch.setenv(key, os.environ.get(key, ''))
    monkeypatch.setattr(sys, 'path', list(sys.path))
    monkeypatch.setattr(host, 'live', host.live)
    main = runpy.run_path(str(repo / script))['main']

    class Stop(Exception):
        pass

    def stop(*args, **kwargs):
        raise Stop
    monkeypatch.setitem(main.__globals__, 'serve', stop)  # after the harness set itself up, before any server starts
    extra = (f'acc-test-isolation-{uuid.uuid4().hex[:8]}',) if 'socket_name' in inspect.signature(main).parameters else ()
    with pytest.raises(Stop):
        await main(tmp_path / 'demo', 18761, *extra)
    first, second = host.live(), host.live()
    assert first['cpu_count'] == second['cpu_count'] == 16 and first['memory']['total'] == 64e9
    assert first['load'] == [0.84, 0.92, 1.05]


@pytest.mark.parametrize('value', [None, '0', '1'])
def test_importing_browser_helpers_preserves_preview_environment(monkeypatch, value):
    """Certificate/helper imports must not enable v2 for unrelated tests in the same worker."""
    for key in ('ACC_ORCHESTRATOR', 'RYNHULT_ORCHESTRATOR'):
        monkeypatch.delenv(key, raising=False)
    if value is not None:
        monkeypatch.setenv('ACC_ORCHESTRATOR', value)
    before = {key: os.environ[key] for key in os.environ if '_ORCHESTRATOR' in key}
    runpy.run_path(str(Path(__file__).resolve().parents[1] / 'tests/e2e_server.py'))
    assert {key: os.environ[key] for key in os.environ if '_ORCHESTRATOR' in key} == before


@pytest.mark.parametrize('override', [None, 'ACC_ORCHESTRATOR', 'RYNHULT_ORCHESTRATOR'])
def test_browser_entrypoint_sets_default_before_slice_registration(tmp_path, override):
    """The real entry point still registers startup-only delivery tools; abort before any service starts."""
    import subprocess

    repo = Path(__file__).resolve().parents[1]
    code = '''import runpy, sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]).parent))
sys.argv = [sys.argv[1], sys.argv[2], '18761']
try:
    runpy.run_path(sys.argv[0], run_name='__main__')
except SystemExit as error:
    assert 'Unix socket paths' in str(error), str(error)
else:
    raise AssertionError('overlong fixture socket should refuse before starting services')
from acc.server import orchestrator
assert orchestrator.orchestrator_enabled() is EXPECTED
assert ('report' in orchestrator.team_fields()) is EXPECTED
'''.replace('EXPECTED', str(override is None))
    env = {key: value for key, value in os.environ.items() if '_ORCHESTRATOR' not in key}
    env['ACC_ORCHESTRATOR_V2_DELIVERY'] = '1'
    if override:
        env[override] = '0'
    result = subprocess.run([sys.executable, '-c', code, str(repo / 'tests/e2e_server.py'),
                             str(tmp_path / ('too-long-' * 30))], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
