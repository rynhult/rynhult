"""Phase-one rename contracts: aliases must not create independent state."""
import os
from pathlib import Path

import pytest


def test_environment_precedence_and_empty(monkeypatch):
    from acc.env import environment
    monkeypatch.setenv('ACC_COMPAT_TEST', 'old')
    monkeypatch.delenv('RYNHULT_COMPAT_TEST', raising=False)
    assert environment.get('ACC_COMPAT_TEST') == 'old'
    monkeypatch.setenv('RYNHULT_COMPAT_TEST', 'new')
    assert environment['ACC_COMPAT_TEST'] == 'new'
    assert environment['RYNHULT_COMPAT_TEST'] == 'new'
    monkeypatch.setenv('RYNHULT_COMPAT_TEST', '')
    assert environment.get('ACC_COMPAT_TEST', 'default') == ''
    assert environment.get('NOT_A_SETTING', 'default') == 'default'


def test_environment_dual_exports_and_mutation(monkeypatch):
    from acc.env import Environment
    values = {'RYNHULT_TEST': 'new', 'ACC_TEST': 'old', 'PATH': '/bin'}
    env = Environment(values)
    assert dict(env) == {'RYNHULT_TEST': 'new', 'ACC_TEST': 'new', 'PATH': '/bin'}
    env['ACC_TEST'] = 'changed'
    assert env['RYNHULT_TEST'] == 'changed'
    del env['ACC_TEST']
    assert 'RYNHULT_TEST' not in env
    assert env['PATH'] == '/bin'


def test_real_settings_and_node_file(monkeypatch, tmp_path):
    from acc.node.config import NodeConfig
    from acc.server.config import Settings
    monkeypatch.setenv('ACC_LISTEN', 'old')
    monkeypatch.setenv('RYNHULT_LISTEN', 'new')
    assert Settings.from_env().listen == 'new'
    path = tmp_path / 'node.env'
    path.write_text('RYNHULT_NODE_PRIMARY=https://main.example.lan\nRYNHULT_NODE_ID=demo\nRYNHULT_NODE_TRUST=system\n')
    assert NodeConfig.load(path).node_id == 'demo'


def test_capability():
    from acc.node.client import CAPABILITIES
    assert 'rename-compat-1' in CAPABILITIES


def test_reserved_session_aliases():
    from acc.broker.ops import RESERVED_ENV
    for suffix in ('RUN_DIR', 'SESSION_ID', 'GUARD_FILES'):
        assert 'RYNHULT_' + suffix in RESERVED_ENV


def test_shell_bridge_preserves_arbitrary_values_and_empty(tmp_path):
    import subprocess
    script = Path(__file__).resolve().parents[1] / 'deploy/lib/acc-env.sh'
    values = {**os.environ, 'ACC_TEST': 'old', 'RYNHULT_TEST': 'a\n$(touch bad) " \'',
              'ACC_EMPTY': 'secret', 'RYNHULT_EMPTY': ''}
    result = subprocess.run(['sh', '-c', '. "$1"; printf "%s|%s|%s" "$ACC_TEST" "$RYNHULT_TEST" "$ACC_EMPTY"',
                             'sh', str(script)], env=values, text=True, capture_output=True, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout == values['RYNHULT_TEST'] + '|' + values['RYNHULT_TEST'] + '|'
    assert not (tmp_path / 'bad').exists()


def test_standalone_backup_uses_new_names(monkeypatch, tmp_path):
    import runpy
    monkeypatch.setenv('RYNHULT_DATA', str(tmp_path))
    script = Path(__file__).resolve().parents[1] / 'deploy/backup/acc-backup'
    assert runpy.run_path(str(script))['DATA'] == tmp_path


def test_unit_aliases_are_idempotent_and_refuse_conflicts(tmp_path):
    import runpy
    module = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'deploy/lib/unit-aliases.py'))
    install = module['install_aliases']
    for name in ('accd.service', 'accd.socket', 'acc-broker@.service', 'acc-backup.timer'):
        (tmp_path / name).write_text('[Unit]\nDescription=Example\n')
    install(tmp_path)
    install(tmp_path)
    assert (tmp_path / 'rynhultd.socket').readlink() == Path('accd.socket')
    assert (tmp_path / 'rynhult-broker@.service').samefile(tmp_path / 'acc-broker@.service')
    (tmp_path / 'rynhultd.service').unlink()
    (tmp_path / 'rynhultd.service').write_text('custom')
    with pytest.raises(ValueError, match='conflict'):
        install(tmp_path)
    assert (tmp_path / 'rynhultd.service').read_text() == 'custom'


def test_unit_aliases_refuse_masks_and_alias_dropins(tmp_path):
    import runpy
    install = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'deploy/lib/unit-aliases.py'))['install_aliases']
    (tmp_path / 'accd.service').symlink_to('/dev/null')
    with pytest.raises(ValueError):
        install(tmp_path)
    assert not (tmp_path / 'rynhultd.service').exists()
    (tmp_path / 'accd.service').unlink()
    (tmp_path / 'accd.service').write_text('[Service]')
    (tmp_path / 'rynhultd.service.d').mkdir()
    with pytest.raises(ValueError):
        install(tmp_path)


def test_javascript_environment_contract():
    import subprocess
    module = (Path(__file__).resolve().parents[1] / 'src/acc/env.mjs').as_uri()
    code = f'''import {{ environmentValue }} from {module!r};
const values = {{ACC_TEST: 'old', RYNHULT_TEST: ''}};
if (environmentValue('ACC_TEST', 'default', values) !== '') process.exit(1);
if (environmentValue('RYNHULT_TEST', 'default', {{ACC_TEST: 'old'}}) !== 'old') process.exit(2);
if (environmentValue('OTHER', 'default', {{}}) !== 'default') process.exit(3);
'''
    result = subprocess.run(['node', '--input-type=module', '-e', code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_shell_env_file_new_empty_wins(tmp_path):
    import subprocess
    script = Path(__file__).resolve().parents[1] / 'deploy/lib/acc-env.sh'
    config = tmp_path / 'config'
    config.write_text('ACC_TEST=old\nRYNHULT_TEST=\n')
    result = subprocess.run(['sh', '-c', '. "$1"; acc_setting_file ACC_TEST "$2"', 'sh', str(script), str(config)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == '\n'


def test_shipped_units_declare_alias_metadata():
    import configparser
    import runpy
    repo = Path(__file__).resolve().parents[1]
    units = runpy.run_path(str(repo / 'deploy/lib/unit-aliases.py'))['UNITS']
    for name in units:
        for path in (repo / 'deploy').rglob(name):
            config = configparser.ConfigParser(interpolation=None, strict=False)
            config.read(path)
            assert config.get('Install', 'Alias', fallback='') == 'rynhult' + name[3:], path


def test_command_aliases_refuse_existing_objects(tmp_path):
    import subprocess
    script = Path(__file__).resolve().parents[1] / 'deploy/lib/acc-env.sh'
    target = tmp_path / 'target'
    target.write_text('managed')
    alias = tmp_path / 'alias'
    def link():
        return subprocess.run(['sh', '-c', '. "$1"; acc_safe_link "$2" "$3"', 'sh',
                               str(script), str(target), str(alias)], capture_output=True, text=True)
    assert link().returncode == 0
    assert link().returncode == 0
    alias.unlink()
    alias.write_text('custom')
    assert link().returncode != 0
    assert alias.read_text() == 'custom'
    alias.unlink()
    alias.symlink_to(tmp_path / 'missing')
    assert link().returncode != 0
    assert alias.readlink() == tmp_path / 'missing'


def test_systemd_enable_aliases_in_offline_root(tmp_path):
    """Exercise actual systemd install metadata without talking to the host manager."""
    import shutil
    import subprocess
    systemctl = shutil.which('systemctl')
    if not systemctl:
        pytest.skip('systemctl unavailable')
    repo = Path(__file__).resolve().parents[1]
    units = tmp_path / 'etc/systemd/system'
    units.mkdir(parents=True)
    for name in ('accd.service', 'accd.socket'):
        shutil.copyfile(repo / 'deploy' / name, units / name)
    for name in ('acc-broker@.service', 'acc-broker@.socket'):
        shutil.copyfile(repo / 'deploy/broker' / name, units / name)
    result = subprocess.run([systemctl, '--root', str(tmp_path), 'enable', 'accd.socket',
                             'acc-broker@demo.service', 'acc-broker@demo.socket'], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    for name in ('accd.socket', 'acc-broker@demo.service', 'acc-broker@demo.socket'):
        canonical = name.replace('@demo.', '@.')
        assert (units / ('rynhult' + name[3:])).readlink() == Path('/etc/systemd/system') / canonical


def test_unit_alias_installer_accepts_systemctl_absolute_alias(tmp_path):
    import runpy
    install = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'deploy/lib/unit-aliases.py'))['install_aliases']
    source = tmp_path / 'accd.service'
    source.write_text('[Service]')
    (tmp_path / 'rynhultd.service').symlink_to(source)
    install(tmp_path)
    assert (tmp_path / 'rynhultd.service').readlink() == source


def test_unit_aliases_preflight_instance_conflicts(tmp_path):
    import runpy
    install = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'deploy/lib/unit-aliases.py'))['install_aliases']
    source = tmp_path / 'acc-broker@.service'
    source.write_text('[Service]')
    instance = tmp_path / 'rynhult-broker@demo.service'
    instance.write_text('custom')
    with pytest.raises(ValueError, match='conflict'):
        install(tmp_path)
    assert not (tmp_path / 'rynhult-broker@.service').exists()
    instance.unlink()
    instance.symlink_to(source)
    install(tmp_path)
    assert (tmp_path / 'rynhult-broker@.service').readlink() == Path('acc-broker@.service')


@pytest.fixture
def mac_account_tools(tmp_path):
    tools = tmp_path / 'tools'
    tools.mkdir()

    def account(uid):
        identity = tools / 'id'
        identity.write_text(
            f'#!/bin/sh\ncase "$1" in\n-u) echo {uid} ;;\n-un) echo demo-agent ;;\n*) exit 1 ;;\nesac\n'
        )
        identity.chmod(0o755)
        return tools

    return account


@pytest.mark.parametrize('uid', [65534, 0], ids=['signed-in-user', 'root-refused'])
def test_mac_dry_run_installs_bridge_for_linked_commands(tmp_path, mac_account_tools, uid):
    import subprocess
    # The installer calls id in a subprocess, so os.getuid monkeypatching cannot reach it.
    tools = mac_account_tools(uid)
    prefix = tmp_path / 'homebrew'
    brew = tools / 'brew'
    brew.write_text(f'#!/bin/sh\nprintf "%s\\n" "{prefix}"\n')
    brew.chmod(0o755)
    repo = Path(__file__).resolve().parents[1]
    result = subprocess.run(['bash', str(repo / 'deploy/node/macos/install.sh'), '--dry-run',
                             '--render-dir', str(tmp_path / 'render')],
                            env={**os.environ, 'HOME': str(tmp_path / 'home'), 'PATH': f"{tools}:{os.environ['PATH']}"},
                            capture_output=True, text=True)
    if uid == 0:
        assert result.returncode == 1
        assert 'run as yourself, not root' in result.stderr
        assert not (tmp_path / 'render').exists()
        return
    assert result.returncode in (0, 2), result.stderr
    assert str(prefix / 'bin/acc-env.sh') in result.stdout


@pytest.mark.skipif(os.geteuid() == 0, reason='probe must stop at the unprivileged install guard')
def test_root_installer_clamps_path_before_loading_bridge(tmp_path):
    import subprocess
    tools = tmp_path / 'tools'
    tools.mkdir()
    marker = tmp_path / 'untrusted-env-ran'
    fake_env = tools / 'env'
    fake_env.write_text(f'#!/bin/sh\n/usr/bin/touch "{marker}"\n')
    fake_env.chmod(0o755)
    repo = Path(__file__).resolve().parents[1]
    result = subprocess.run(['bash', str(repo / 'deploy/install.sh')],
                            env={**os.environ, 'PATH': f'{tools}:/usr/bin:/bin'}, capture_output=True, text=True)
    assert result.returncode != 0 and 'run as root' in result.stderr
    assert not marker.exists()


def test_unit_alias_preflight_does_not_write(tmp_path):
    import runpy
    install = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'deploy/lib/unit-aliases.py'))['install_aliases']
    (tmp_path / 'accd.service').write_text('[Service]')
    install(tmp_path, check_only=True)
    assert sorted(p.name for p in tmp_path.iterdir()) == ['accd.service']
    (tmp_path / 'acc-update.service').symlink_to('/dev/null')
    with pytest.raises(ValueError):
        install(tmp_path, check_only=True)
    assert (tmp_path / 'acc-update.service').readlink() == Path('/dev/null')


@pytest.mark.parametrize('prefix', ['ACC', 'RYNHULT'])
@pytest.mark.parametrize('suffix', ['RUN_DIR', 'SESSION_ID', 'GUARD_FILES'])
def test_compiler_refuses_guard_overrides_before_older_brokers(monkeypatch, prefix, suffix):
    from types import SimpleNamespace

    from acc.server import compiler
    provider = SimpleNamespace(env=lambda *args, **kwargs: {f'{prefix}_{suffix}': 'untrusted'})
    monkeypatch.setattr(compiler.providers, 'get', lambda name: provider)
    with pytest.raises(compiler.CompileError, match='reserved'):
        compiler.session_environment(compiler.LaunchInputs(provider='claude'), None)
