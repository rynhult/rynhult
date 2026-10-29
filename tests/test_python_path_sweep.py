"""Subprocess regressions: hostile checkout/import environment, never live agent tools."""
import asyncio
import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import venv
from pathlib import Path

import pytest

from acc.broker import attach, containers, ops
from acc.server import vault

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(params=["cwd-json", "cwd-package", "environment", "pythonhome"])
def planted(tmp_path, request):
    checkout = tmp_path / 'checkout'
    checkout.mkdir()
    marker = tmp_path / 'imported'
    payload = f'open({str(marker)!r}, "w").write("injected")\nraise RuntimeError("planted module")\n'
    for folder in (checkout, tmp_path / 'search', tmp_path / f'user/lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages'):
        folder.mkdir(parents=True, exist_ok=True)
        for name in ('json.py', 'sitecustomize.py', 'usercustomize.py', 'platform.py'):
            (folder / name).write_text(payload)
        if request.param != 'cwd-json':
            (folder / 'acc').mkdir()
            (folder / 'acc/__init__.py').write_text(payload)
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(tmp_path / 'empty-home'), 'LANG': 'C.UTF-8'}
    if request.param == 'environment':
        env.update(PYTHONPATH=str(tmp_path / 'search'), PYTHONUSERBASE=str(tmp_path / 'user'))
    if request.param == 'pythonhome':
        env['PYTHONHOME'] = str(tmp_path / 'not-a-runtime')
    return checkout, marker, env


def test_explanation_pane_does_not_import_from_workspace(env, monkeypatch, planted, tmp_path):
    cwd, marker, child_env = planted
    config = env['broker']
    acc_id = 'abcdef123456'
    workspace = config.run_dir / acc_id / 'workspace'
    workspace.parent.mkdir(parents=True)
    shutil.copytree(cwd, workspace)
    calls = []

    def tmux(config, *args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1 if args[0] == 'has-session' else 0,
                                           '123\n' if args[0] == 'list-panes' else '', '')

    monkeypatch.setattr(ops, 'tmux', tmux)
    result = ops.launch(config, {'acc_id': acc_id, 'tmux_name': 'acc-sweep', 'provider': 'claude',
                                'args': [], 'cwd': str(workspace), 'files': {'explanation.json': '{}'}, 'env': {}})
    # Missing spec fields deliberately stop the real wrapper before any provider/auth/hook execution.
    child = subprocess.run(shlex.split(result['command']), cwd=workspace, env=child_env,
                           capture_output=True, text=True, timeout=10)
    assert not marker.exists(), child.stderr
    assert 'planted module' not in child.stderr
    assert (workspace.parent / 'explanation-status.json').exists(), child.stderr
    assert all(not any(arg.startswith('PYTHONPATH=') for arg in call) for call in calls)


@pytest.mark.asyncio
async def test_pty_attach_child_ignores_cwd_and_import_environment(env, monkeypatch, planted):
    cwd, marker, child_env = planted
    fake = cwd / 'tmux'
    fake.write_text('#!/bin/sh\nprintf "SWEEP_FAKE_TMUX\\n"\n')
    fake.chmod(0o700)
    child_env['PATH'] = str(cwd) + ':/usr/bin:/bin'
    monkeypatch.chdir(cwd)
    for key in tuple(os.environ):
        if key.startswith('PYTHON'):
            monkeypatch.delenv(key)
    for key, value in child_env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(ops, 'tmux', lambda *args: subprocess.CompletedProcess([], 0, '', ''))
    reader = asyncio.StreamReader()

    class Writer:
        data = b''
        def write(self, data):
            self.data += data
        async def drain(self):
            pass

    writer = Writer()
    await asyncio.wait_for(attach.attach(env['broker'], {'tmux_name': 'acc-sweep'}, reader, writer), 10)
    assert not marker.exists(), writer.data
    assert b'SWEEP_FAKE_TMUX' in writer.data


def test_container_git_child_ignores_pythonpath(planted):
    cwd, marker, child_env = planted
    # Private repository + container marker, so this exercises the production host Git wrapper.
    subprocess.run(['git', 'init', '-q', str(cwd)], check=True)
    checkout = cwd.parent / '.worktrees' / 'private'
    checkout.parent.mkdir()
    cwd.rename(checkout)
    command = containers.git_guard(checkout)
    child = subprocess.run([*command, 'rev-parse', '--show-toplevel'], cwd=checkout,
                           env=child_env, capture_output=True, text=True, timeout=10)
    assert not marker.exists(), child.stderr
    assert child.returncode == 0, child.stderr
    assert child.stdout.strip() == str(checkout)


def test_pip_tooling_ignores_callers_checkout(tmp_path, monkeypatch, planted):
    from acc import tooling
    cwd, marker, _ = planted
    home = tmp_path / 'home'
    runtime = home / '.local'
    venv.EnvBuilder(with_pip=False).create(runtime)
    site = next((runtime / 'lib').glob('python*/site-packages'))
    (site / 'pip').mkdir()
    (site / 'pip/__init__.py').write_text('')
    (site / 'pip/__main__.py').write_text('import json,sys\nprint(json.dumps([{ "name":"sweep-package", "version":"1.0" }]) if "list" in sys.argv else "fake install")\n')
    monkeypatch.chdir(cwd)
    result = tooling.execute('pip', 'sweep-package', '1.0', 'install', home=home, system=False)
    assert not marker.exists()
    assert result['version'] == '1.0'


@pytest.mark.parametrize('name', ['PYTHONPATH', 'PYTHONHOME', 'PYTHONUSERBASE', 'PYTHONSTARTUP', 'PYTHONINSPECT',
                                 'NODE_PATH', 'NODE_OPTIONS', 'BASH_ENV', 'ENV'])
def test_project_loader_variables_refused_at_both_boundaries(name, env):
    with pytest.raises(vault.VaultError):
        vault.check_variable(name)
    with pytest.raises(ops.OpError, match='not allowed'):
        ops.launch(env['broker'], {'acc_id': 'abcdef123456', 'tmux_name': 'acc-sweep', 'provider': 'claude',
                                  'args': [], 'cwd': str(env['workspaces'] / 'demo'), 'files': {}, 'env': {name: '/tmp/untrusted'}})


def test_public_snapshot_does_not_import_callers_modules(planted):
    cwd, marker, child_env = planted
    result = subprocess.run(['bash', str(REPO / 'scripts/public-snapshot.sh')], cwd=cwd,
                            env=child_env, capture_output=True, text=True, timeout=10)
    assert not marker.exists(), result.stderr
    assert 'usage:' in result.stderr


@pytest.fixture
def fake_acc_runtime(tmp_path):
    runtime = tmp_path / 'trusted-runtime'
    venv.EnvBuilder(with_pip=False).create(runtime)
    site = next((runtime / 'lib').glob('python*/site-packages'))
    package = site / 'acc'
    package.mkdir()
    (package / '__init__.py').write_text('')
    for name in ('broker', 'server'):
        (package / name).mkdir()
        (package / name / '__init__.py').write_text('')
    for module in ('node.py', 'broker/server.py', 'server/app.py'):
        (package / module).write_text('import json,sys\nprint(json.dumps({"trusted":True,"isolated":sys.flags.isolated,"args":sys.argv[1:]}))\n')
    return runtime


@pytest.mark.parametrize('unit', ['deploy/accd.service', 'deploy/node/acc-node.service',
                                 'deploy/broker/acc-broker@.service', 'deploy/node/acc-broker@.service.d-node.conf',
                                 'deploy/node/macos/acc.plist.in', 'deploy/node/macos/acc-node'])
def test_installed_entrypoints_ignore_planted_modules(unit, fake_acc_runtime, planted, tmp_path):
    cwd, marker, child_env = planted
    runtime = fake_acc_runtime
    if unit.endswith('acc-node'):
        script = tmp_path / 'bin/acc-node'
        script.parent.mkdir()
        shutil.copy2(REPO / unit, script)
        shutil.copy2(REPO / 'deploy/lib/acc-env.sh', script.with_name('acc-env.sh'))
        home = tmp_path / 'owner'
        app = home / 'Library/Application Support/ACC/app'
        app.mkdir(parents=True)
        (app / '.venv').symlink_to(runtime, target_is_directory=True)
        child_env['HOME'] = str(home)
        argv = [str(script), 'status']
    elif unit.endswith('.in'):
        plist = plistlib.loads((REPO / unit).read_bytes())
        argv = [str(runtime / 'bin/python'), *plist['ProgramArguments'][1:]]
        argv[-1] = 'acc.broker.server'
    else:
        line = next(line for line in (REPO / unit).read_text().splitlines() if line.startswith('ExecStart=') and 'python' in line)
        argv = shlex.split(line.partition('=')[2])
        argv[0] = str(runtime / 'bin/python')
    result = subprocess.run(argv, cwd=cwd, env=child_env, capture_output=True, text=True, timeout=10)
    assert not marker.exists(), result.stderr
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['isolated'] == 1


def test_node_path_rejection_prevents_loading_planted_package(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node not installed; loader-variable validation still covered')
    package = tmp_path / 'sweep-package'
    package.mkdir()
    marker = tmp_path / 'loaded'
    (package / 'index.js').write_text(f'require("fs").writeFileSync({json.dumps(str(marker))}, "injected");')
    # Control demonstrates a real Node loader, not just a reserved-name string assertion.
    result = subprocess.run([node, '-e', 'require("sweep-package")'], cwd=tmp_path,
                            env={'PATH': '/usr/bin:/bin', 'NODE_PATH': str(tmp_path)},
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert marker.exists()
    marker.unlink()
    with pytest.raises(vault.VaultError):
        vault.check_variable('NODE_PATH')
    assert not marker.exists()


def test_macos_smoke_planner_ignores_planted_modules(planted):
    cwd, marker, child_env = planted
    result = subprocess.run(['bash', str(REPO / 'desktop/tests/mac-phase1-smoke.sh'), '--python', sys.executable,
                             '--owner', 'alex', '--owner-uid', '1000', '--owner-gid', '1000'],
                            cwd=cwd, env=child_env, capture_output=True, text=True, timeout=10)
    assert not marker.exists(), result.stderr
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['agent'] == 'rynd-1000'


def launch_with(env, variables):
    return ops.launch(env['broker'], {'acc_id': 'abcdef123456', 'tmux_name': 'acc-sweep', 'provider': 'claude',
                                      'args': [], 'cwd': str(env['workspaces'] / 'demo'), 'files': {}, 'env': variables})


@pytest.mark.parametrize('name', ['PYTHONWARNINGS', 'PYTHONBREAKPOINT', 'PYTHONPYCACHEPREFIX', 'PYTHON_KEYRING_BACKEND',
                                 'LD_AUDIT', 'LD_PRELOAD', 'DYLD_INSERT_LIBRARIES', 'DYLD_LIBRARY_PATH', 'PERL5OPT',
                                 'PERL5LIB', 'RUBYOPT', 'JAVA_TOOL_OPTIONS', 'NODE_REPL_EXTERNAL_MODULE', 'ZDOTDIR'])
def test_loader_families_refused_at_both_boundaries(name, env):
    """Review 2026-10-25: the broker re-check had no LD_ prefix or PERL5OPT, neither boundary knew macOS DYLD_, and
    only five of Python's variables were named."""
    with pytest.raises(vault.VaultError):
        vault.check_variable(name)
    with pytest.raises(ops.OpError, match='not allowed'):
        launch_with(env, {name: '/tmp/untrusted'})


@pytest.mark.parametrize('name', ['NODE_AUTH_TOKEN', 'NODE_ENV', 'OPENAI_API_KEY', 'PYPI_TOKEN'])
def test_ordinary_credentials_stay_allowed(name):
    assert vault.check_variable(name) == name
    assert not ops.interpreter_variable(name)



def test_python_child_puts_the_standard_library_first_and_never_the_checkout(tmp_path, planted):
    """Review 2026-10-25 (L-1): the ACC root went before the standard library; a json.py there shadowed it."""
    from acc.python_child import module_command
    checkout, marker, env = planted
    root = tmp_path / 'root'
    root.mkdir()
    (root / 'json.py').write_text(f'open({str(marker)!r}, "w").write("x")\n')
    (root / 'sweep_probe.py').write_text(
        'import json, site, sys, sysconfig\n'
        'print(json.dumps({"json": json.__file__, "path": sys.path, "site": site.getsitepackages(),\n'
        '                  "stdlib": sysconfig.get_paths()["stdlib"]}))\n')
    command = module_command('sweep_probe')
    command[command.index(str(REPO / 'src'))] = str(root)
    result = subprocess.run(command, cwd=checkout, env=env, capture_output=True, text=True, timeout=30)
    if env.get('PYTHONHOME'):
        assert result.returncode == 0, result.stderr  # -I ignores it too
    probe = json.loads(result.stdout)
    assert not marker.exists() and not probe['json'].startswith(str(root))
    path = probe['path']
    assert '' not in path and str(checkout) not in path
    stdlib = path.index(probe['stdlib'])
    assert probe['json'].startswith(probe['stdlib'])
    assert stdlib < path.index(str(root)) < min((path.index(p) for p in probe['site'] if p in path), default=len(path))


GIT_COMMAND_VARIABLES = ['GIT_SSH', 'GIT_SSH_COMMAND', 'GIT_PROXY_COMMAND', 'GIT_ASKPASS', 'SSH_ASKPASS', 'GIT_EDITOR',
                         'GIT_SEQUENCE_EDITOR', 'GIT_PAGER', 'GIT_EXTERNAL_DIFF', 'GIT_EXEC_PATH', 'GIT_TEMPLATE_DIR',
                         'GIT_CONFIG', 'GIT_CONFIG_GLOBAL', 'GIT_CONFIG_SYSTEM', 'GIT_CONFIG_NOSYSTEM',
                         'GIT_CONFIG_PARAMETERS', 'GIT_CONFIG_COUNT', 'GIT_CONFIG_KEY_0', 'GIT_CONFIG_VALUE_12',
                         'GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_ALLOW_PROTOCOL', 'EDITOR', 'VISUAL',
                         'PAGER', 'LESSOPEN']


@pytest.mark.parametrize('name', GIT_COMMAND_VARIABLES)
def test_git_command_variables_refused_at_both_boundaries(name, env):
    """Review 2026-10-25 (L-2): a key could make any git in the session run a command or read other configuration."""
    with pytest.raises(vault.VaultError):
        vault.check_variable(name)
    with pytest.raises(ops.OpError, match='not allowed'):
        launch_with(env, {name: '/tmp/untrusted'})


@pytest.mark.parametrize('name', ['GIT_AUTHOR_NAME', 'GIT_AUTHOR_EMAIL', 'GIT_COMMITTER_NAME', 'GIT_COMMITTER_EMAIL',
                                 'GITHUB_TOKEN', 'GITLAB_TOKEN'])
def test_ordinary_git_settings_stay_allowed(name):
    assert vault.check_variable(name) == name
    assert not ops.interpreter_variable(name)


def test_git_config_variables_really_run_a_command(tmp_path):
    """Why GIT_CONFIG_* is reserved: core.sshCommand from the environment runs as soon as git talks to a remote."""
    marker = tmp_path / 'ran'
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(tmp_path), 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_COUNT': '1',
           'GIT_CONFIG_KEY_0': 'core.sshCommand', 'GIT_CONFIG_VALUE_0': f'touch {shlex.quote(str(marker))}; false'}
    subprocess.run(['git', 'ls-remote', 'ssh://example.invalid/repo'], cwd=tmp_path, env=env, capture_output=True,
                   timeout=30)
    assert marker.exists()
    with pytest.raises(vault.VaultError):
        vault.check_variable('GIT_CONFIG_KEY_0')
