"""Bounded installer checks; never runs the live installer or root service."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('folder_install', REPO / 'deploy/folders/install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


@pytest.fixture(autouse=True)
def protected_path_fixture(tmp_path, monkeypatch):
    # No installed host paths are read. Only test-owned paths have real metadata.
    real_lstat = installer.protected_lstat
    def fixture_lstat(path):
        if Path(path).is_relative_to(tmp_path) or Path(path) in tmp_path.parents:
            return real_lstat(path)
        raise FileNotFoundError(path)
    monkeypatch.setattr(installer, 'protected_lstat', fixture_lstat)


def test_provisioning_exact_account_and_protected_locations():
    records = {'alex': (1001, '/home/alex'), 'alex-agent': (1002, '/home/alex-agent'), 'acc': (991, '/srv/custom/data')}
    def lookup(name):
        uid, home = records[name]
        return SimpleNamespace(pw_uid=uid, pw_dir=home)
    result = installer.mapping({'ACC_DATA_DIR': '/mnt/private/data', 'ACC_AGENT_HOME': '/home/alex-agent'},
        {'alex-agent': {'ACC_BROKER_ROLE': 'agents', 'ACC_ALLOWED_ROOTS': '/srv/workspaces:/srv/old'},
         'alex': {'ACC_BROKER_ROLE': 'services'}}, 'alex', 'acc', lookup=lookup)
    assert result['policy_qualified'] is True
    assert result['qualified'] is False and result['shared_group_qualified'] is False
    assert list(result['accounts']) == ['alex-agent']
    account = result['accounts']['alex-agent']
    assert account['roots'] == ['/srv/workspaces', '/srv/old']
    assert account['homes'] == ['/home/alex', '/home/alex-agent']
    assert '/mnt/private/data' in account['protected']
    assert '/srv/custom/data' in account['protected']
    assert '/home/alex-agent' not in account['protected']
    with pytest.raises(ValueError, match='separate'):
        installer.mapping({}, {'alex': {'ACC_BROKER_ROLE': 'agents'}}, 'alex', 'acc', lookup=lookup)


def test_installer_copies_fixed_modules_and_enables_socket_after_accounts():
    source = (REPO / 'deploy/install.sh').read_text()
    assert source.index('"$REPO/deploy/folders/install.py"') > source.index('/usr/local/sbin/acc-agent-share "$AGENT_USER"')
    assert '--owner "$OWNER" --agent "$AGENT_USER"' in source
    assert 'systemctl enable --now acc-folder-policy.socket' in source
    for name in installer.MODULES:
        assert (REPO / 'src/acc' / name).is_file()
    # Both dashboard and offline main updates use setup, which runs install.sh.
    assert 'deploy/setup.sh' in (REPO / 'src/acc/update_helper.py').read_text()
    assert 'install.sh' in (REPO / 'deploy/setup.sh').read_text()


def test_publish_replaces_link_without_touching_target(tmp_path):
    target = tmp_path / 'unrelated'
    target.write_text('keep')
    destination = tmp_path / 'worker.json'
    destination.symlink_to(target)
    installer.publish(destination, b'{}', 0o600)
    assert target.read_text() == 'keep'
    assert not destination.is_symlink()
    assert destination.stat().st_mode & 0o777 == 0o600


def test_fresh_install_and_upgrade_preserve_owner_mapping(tmp_path, monkeypatch):
    import json
    # Execute the provisioner with all installed paths redirected to this fixture.
    def local_directory(path, mode=0o755):
        path = Path(path)
        if not path.is_relative_to(tmp_path):
            path = tmp_path / str(path).lstrip('/')
        path.mkdir(parents=True, exist_ok=True, mode=mode)
        return path
    monkeypatch.setattr(installer, 'directory', local_directory)
    configuration = installer.configuration
    monkeypatch.setattr(installer, 'configuration', lambda *args: configuration(*args, root=tmp_path))
    monkeypatch.setattr(installer.os, 'geteuid', lambda: 0)
    broker = local_directory('/etc/acc/broker') / 'alex-agent.env'
    broker.write_text('fixture')
    monkeypatch.setattr(installer, 'settings', lambda path: (
        {'ACC_BROKER_ROLE': 'agents', 'ACC_ALLOWED_ROOTS': '/srv/workspaces'}
        if str(path).endswith('.env') else {}))
    generated = {'machine_id': 'local', 'service_uid': 991, 'policy_qualified': True,
                 'qualified': False, 'protected': ['/srv/acc'],
                 'accounts': {'alex-agent': {'roots': ['/srv/workspaces'], 'protected': ['/srv/acc']}}}
    monkeypatch.setattr(installer, 'mapping', lambda *args: generated.copy())
    installer.install(REPO, 'alex', 'alex-agent')
    config = tmp_path / 'etc/acc/folders/worker.json'
    assert json.loads(config.read_text()) == generated
    assert config.stat().st_mode & 0o777 == 0o600
    for name in installer.MODULES:
        assert (tmp_path / 'usr/local/lib/acc-folders/acc' / name).read_bytes() == (REPO / 'src/acc' / name).read_bytes()
    assert (tmp_path / 'etc/systemd/system/acc-folder-policy.socket').is_file()
    revised = {**generated, 'policy_qualified': False, 'accounts': {'reviewed-agent': {'uid': 1234}}}
    config.write_text(json.dumps(revised))
    monkeypatch.setattr(installer, 'read_private', lambda path: Path(path).read_text())
    generated['protected'] = ['/srv/acc', '/mnt/new-backup']
    generated['accounts'] = {}  # no live broker is needed to refresh an old association
    installer.install(REPO, 'alex', 'alex-agent')
    revised['protected'] = ['/mnt/new-backup', '/srv/acc']
    revised['accounts']['reviewed-agent']['protected'] = ['/mnt/new-backup', '/srv/acc']
    assert json.loads(config.read_text()) == revised


def test_account_upgrade_retains_existing_roots(tmp_path):
    source = (REPO / 'deploy/broker/acc-agent-add').read_text()
    assert '--roots) roots=$2; roots_explicit=1' in source
    assert 'if [ -z "$roots_explicit" ] && [ -f "$env" ]' in source
    assert 'roots=$(acc_setting_file ACC_ALLOWED_ROOTS "$env")' in source
    import subprocess
    config = tmp_path / 'broker.env'
    config.write_text('ACC_ALLOWED_ROOTS=/home/alex/repos:/srv/workspaces\n')
    section = '#' + source.split('# An ordinary installer upgrade', 1)[1].split('\n{', 1)[0]
    for explicit, expected in [('', '/home/alex/repos:/srv/workspaces'), ('1', '/chosen/repo')]:
        program = 'set -eu\nroots=/chosen/repo\nacc_setting_file() { cut -d= -f2 "$2"; }\n'
        program += 'env=$1\nroots_explicit=$2\n' + section + '\nprintf "%s" "$roots"\n'
        result = subprocess.run(['bash', '-c', program, 'fixture', str(config), explicit],
                                capture_output=True, text=True, check=True)
        assert result.stdout == expected


@pytest.mark.parametrize('home', ['/', '/home', '/opt', '/mnt', '/etc/alex', '/root/alex',
    '/srv/acc', '/srv/acc/agent', '/home/private/service', '/home/private/service/agent', '/home/../alex', 'relative/home'])
@pytest.mark.parametrize('role', ['alex', 'alex-agent'])
def test_rejects_unsafe_owner_and_agent_homes(home, role):
    records = {'alex': (1001, '/home/alex'), 'alex-agent': (1002, '/home/alex-agent'), 'acc': (991, '/srv/acc')}
    records[role] = (records[role][0], home)
    def lookup(name):
        uid, directory = records[name]
        return SimpleNamespace(pw_uid=uid, pw_dir=directory)
    with pytest.raises(ValueError, match='home'):
        installer.mapping({'ACC_DATA_DIR': '/home/private/service'},
            {'alex-agent': {'ACC_BROKER_ROLE': 'agents', 'ACC_ALLOWED_ROOTS': '/srv/workspaces'}},
            'alex', 'acc', lookup=lookup)


def test_deleted_broker_user_warns_and_protected_entries_are_absolute(capsys):
    def lookup(name):
        if name == 'deleted-agent':
            raise KeyError(name)
        return SimpleNamespace(pw_uid=1001 if name == 'alex' else 991,
                               pw_dir='/home/alex' if name == 'alex' else '/srv/acc')
    result = installer.mapping({'ACC_STORAGE': ':relative:/mnt/private::relative:/mnt/other:'},
        {'deleted-agent': {'ACC_BROKER_ROLE': 'agents'}}, 'alex', 'acc', lookup=lookup)
    assert result['accounts'] == {}
    assert '/mnt/private' in result['protected'] and '/mnt/other' in result['protected']
    assert all(p.startswith('/') for p in result['protected'])
    assert 'skipping broker configuration for deleted user' in capsys.readouterr().err


def test_upgrade_protection_is_additive_for_all_existing_associations():
    existing = {'accounts': {'custom-agent': {'homes': ['/home/custom'], 'roots': ['/old/root'],
                                            'protected': ['/old/protected']}}}
    generated = {'protected': ['/new/protected'], 'accounts': {}}
    merged = installer.merge_protected(existing, generated)
    assert merged['accounts'] == {'custom-agent': {'homes': ['/home/custom'], 'roots': ['/old/root'],
        'protected': ['/new/protected', '/old/protected']}}
    assert installer.merge_protected(merged, generated) == merged


def test_installer_system_paths_match_worker():
    from acc.folder_policy import SYSTEM
    assert installer.SYSTEM == SYSTEM


def test_worker_unit_limits_privilege_without_changing_mount_identities():
    import configparser
    unit = configparser.ConfigParser(interpolation=None)
    unit.read(REPO / 'deploy/folders/acc-folder-policy@.service')
    service = unit['Service']
    assert service['SystemCallFilter'] == '@system-service'
    assert set(service['CapabilityBoundingSet'].split()) == {
        'CAP_DAC_OVERRIDE', 'CAP_DAC_READ_SEARCH', 'CAP_FOWNER', 'CAP_CHOWN',
        'CAP_SETUID', 'CAP_SETGID', 'CAP_KILL'}
    for setting in ('NoNewPrivileges', 'RestrictNamespaces', 'RestrictRealtime',
                    'MemoryDenyWriteExecute', 'ProtectClock', 'ProtectHostname'):
        assert service[setting] == 'yes'
    assert service['PrivateTmp'] == 'no'
    assert service['RestrictAddressFamilies'] == 'AF_UNIX'


@pytest.mark.parametrize('role', ['alex', 'alex-agent'])
def test_backup_mirror_inside_home_is_retained_and_denied(role):
    from acc.folder_policy import FolderError, validate_path

    records = {'alex': (1001, '/home/alex'), 'alex-agent': (1002, '/home/alex-agent'), 'acc': (991, '/srv/acc')}
    def lookup(name):
        uid, home = records[name]
        return SimpleNamespace(pw_uid=uid, pw_dir=home)
    home = records[role][1]
    mirror = home + '/backups/project'
    if role == 'alex-agent':
        with pytest.raises(ValueError, match='home overlaps'):
            installer.mapping({'ACC_BACKUP_MIRRORS': f'alex:/srv/projects/backups:{mirror}'},
                {'alex-agent': {'ACC_BROKER_ROLE': 'agents', 'ACC_ALLOWED_ROOTS': '/srv/workspaces'}},
                'alex', 'acc', lookup=lookup)
        return
    result = installer.mapping({'ACC_BACKUP_MIRRORS': f'alex:/srv/projects/backups:{mirror}'},
        {'alex-agent': {'ACC_BROKER_ROLE': 'agents', 'ACC_ALLOWED_ROOTS': '/srv/workspaces'}},
        'alex', 'acc', lookup=lookup)
    account = result['accounts']['alex-agent']
    assert mirror in account['protected']
    assert validate_path(home + '/repos/project', homes=account['homes'], protected=account['protected'])
    for candidate in (mirror, mirror + '/data', home + '/backups', home):
        with pytest.raises(FolderError, match='cannot be allowed'):
            validate_path(candidate, homes=account['homes'], protected=account['protected'])
    existing = {'accounts': {'retired-agent': {'homes': [home], 'roots': [home + '/repos']}}}
    merged = installer.merge_protected(existing, result)
    assert mirror in merged['accounts']['retired-agent']['protected']
    with pytest.raises(ValueError, match='home overlaps'):
        installer.merge_protected({'accounts': {'retired-agent': {'homes': [mirror + '/agent']}}}, result)


@pytest.mark.parametrize('refusal_stage', ['mapping', 'merge'])
def test_dry_run_uses_real_configuration_and_merge_without_writes(tmp_path, refusal_stage):
    import json
    import subprocess

    root = tmp_path / 'host'
    (root / 'etc/default').mkdir(parents=True)
    (root / 'etc/acc/folders').mkdir(parents=True)
    (root / 'etc/acc/broker').mkdir()
    (root / 'etc/default/accd').write_text('ACC_SERVICES_ACCOUNT=alex\n')
    (root / 'etc/default/acc-backup').write_text(
        'ACC_BACKUP_MIRRORS=alex:/srv/projects/backups:/home/alex/backups/project\n')
    path = root / 'etc/acc/folders/worker.json'
    original = {'machine_id': 'local', 'service_uid': 991,
                'accounts': {'retired-agent': {'homes': ['/home/alex'], 'roots': ['/home/alex/repos']}}}
    path.write_text(json.dumps(original))
    path.chmod(0o600)
    # Stage the actual shell and CLI; only host reads and the unrelated alias
    # helper are replaced. Both publication primitives fail if invoked.
    staged = tmp_path / 'repo/deploy'
    (staged / 'folders').mkdir(parents=True)
    (staged / 'lib').mkdir()
    (staged / 'lib/public-aliases.py').write_text('# Alias validation is outside this fixture.\n')
    shell = staged / 'install.sh'
    shell.write_text((REPO / 'deploy/install.sh').read_text())
    source = (REPO / 'deploy/folders/install.py').read_text()
    source = source.replace('lookup=pwd.getpwnam',
        'lookup=lambda name: type("User", (), dict(pw_uid=1001 if name == "alex" else 991, '
        'pw_dir="/home/alex" if name == "alex" else "/srv/acc"))()')
    source = source.replace("if __name__ == '__main__':",
        'def no_write(*args, **kwargs):\n'
        '    raise AssertionError("write attempted")\n'
        'directory = publish = no_write\n'
        'def missing_path(path):\n'
        '    raise FileNotFoundError(path)\n'
        'protected_lstat = missing_path\n'
        'read_private = lambda path: Path(path).read_text()\n'
        "if __name__ == '__main__':")
    (staged / 'folders/install.py').write_text(source)
    before = {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    passed = subprocess.run(['bash', str(shell), '--dry-run', '--alias-root', str(root)], capture_output=True, text=True)
    assert passed.returncode == 0, passed.stderr
    assert 'Folder mapping validation passed' in passed.stdout
    assert before == {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    if refusal_stage == 'merge':
        original['accounts']['retired-agent']['homes'] = ['/srv/acc/agent']
        path.write_text(json.dumps(original))
    else:
        (root / 'etc/default/accd').write_text('ACC_SERVICES_ACCOUNT=alex\nACC_DATA_DIR=/home/alex\n')
    before = {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    refused = subprocess.run(['bash', str(shell), '--dry-run', '--alias-root', str(root)], capture_output=True, text=True)
    assert refused.returncode != 0
    assert 'home overlaps a protected or system folder' in refused.stderr
    assert before == {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_upgrade_refuses_protected_descendant_of_agent_home():
    existing = {'accounts': {'retired-agent': {'homes': ['/home/alex', '/home/alex-agent']}}}
    with pytest.raises(ValueError, match='home overlaps'):
        installer.merge_protected(existing, {'protected': ['/home/alex-agent/dump']})


@pytest.mark.parametrize('spelling', ['x/../backups', './backups', 'x//backups', 'backups/', 'link', 'link/backups'])
@pytest.mark.parametrize('setting', ['ACC_BACKUP_MIRRORS', 'ACC_DATA_DIR'])
def test_install_and_dry_run_refuse_ambiguous_protected_paths(tmp_path, monkeypatch, spelling, setting):
    (tmp_path / 'link').symlink_to(tmp_path / 'missing', target_is_directory=True)
    bad = str(tmp_path) + '/' + spelling
    config = {'ACC_SERVICES_ACCOUNT': 'alex', setting:
              f'alex:/srv/source:{bad}' if setting == 'ACC_BACKUP_MIRRORS' else bad}
    monkeypatch.setattr(installer, 'settings', lambda path: config)
    mapping = installer.mapping
    def lookup(name):
        return SimpleNamespace(pw_uid=1001 if name == 'alex' else 991,
                               pw_dir='/home/alex' if name == 'alex' else '/srv/acc')
    monkeypatch.setattr(installer, 'mapping', lambda *args: mapping(*args, lookup=lookup))
    configuration = installer.configuration
    monkeypatch.setattr(installer, 'configuration', lambda *args: configuration(*args, root=tmp_path))
    monkeypatch.setattr(installer.os, 'geteuid', lambda: 0)
    def no_write(*args, **kwargs):
        pytest.fail('publication attempted before validation')
    monkeypatch.setattr(installer, 'directory', no_write)
    monkeypatch.setattr(installer, 'publish', no_write)
    with pytest.raises(ValueError, match=setting):
        installer.configuration('alex', 'alex-agent')
    with pytest.raises(ValueError, match=setting):
        installer.install(REPO, 'alex', 'alex-agent')


def test_origin_urls_in_settings_are_not_protected_paths():
    records = {'alex': (1001, '/home/alex'), 'alex-agent': (1002, '/home/alex-agent'), 'acc': (991, '/srv/acc')}
    def lookup(name):
        uid, home = records[name]
        return SimpleNamespace(pw_uid=uid, pw_dir=home)
    result = installer.mapping({'ACC_ALLOWED_ORIGINS': 'https://acc.example.net,http://localhost:8443',
                                'ACC_DATA_DIR': '/srv/acc/data'},
        {'alex-agent': {'ACC_BROKER_ROLE': 'agents', 'ACC_ALLOWED_ROOTS': '/srv/workspaces'}},
        'alex', 'acc', lookup=lookup)
    protected = result['accounts']['alex-agent']['protected']
    assert '/srv/acc/data' in protected
    assert not any(p.startswith('//') for p in protected)


@pytest.mark.parametrize('relative', [
    'etc/default/accd', 'etc/default/acc-backup', 'etc/acc/broker/alex-agent.env',
    'etc/acc/folders/worker.json', 'etc/acc/broker',
])
def test_dry_run_skips_permission_denied_configuration(tmp_path, monkeypatch, capsys, relative):
    import errno

    primary = tmp_path / 'etc/default/accd'
    primary.parent.mkdir(parents=True)
    primary.write_text('ACC_SERVICES_ACCOUNT=alex\n')
    backup = tmp_path / 'etc/default/acc-backup'
    backup.write_text('')
    broker = tmp_path / 'etc/acc/broker/alex-agent.env'
    broker.parent.mkdir(parents=True)
    broker.write_text('')
    worker = tmp_path / 'etc/acc/folders/worker.json'
    worker.parent.mkdir(parents=True)
    worker.write_text('{}')
    denied = tmp_path / relative
    real_iterdir = Path.iterdir

    def read(path):
        if path == denied:
            raise PermissionError(errno.EACCES, 'Permission denied', str(path))
        return path.read_text()

    def iterdir(path):
        if path == denied:
            raise PermissionError(errno.EACCES, 'Permission denied', str(path))
        return real_iterdir(path)

    before = {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    monkeypatch.setattr(installer, 'read_private', read)
    monkeypatch.setattr(Path, 'iterdir', iterdir)
    monkeypatch.setattr(installer, 'mapping', lambda *args: {})
    installer.dry_run(root=tmp_path)
    assert capsys.readouterr().out == f'folder mapping: skipped: needs root to read {denied}\n'
    monkeypatch.setattr(Path, 'iterdir', real_iterdir)
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}


def test_dry_run_unreadable_primary_without_root(tmp_path, capsys):
    import os

    if os.geteuid() == 0:
        pytest.skip('real permission denial requires an unprivileged user')
    path = tmp_path / 'etc/default/accd'
    path.parent.mkdir(parents=True)
    path.write_text('ACC_SERVICES_ACCOUNT=alex\n')
    path.chmod(0)
    try:
        installer.dry_run(root=tmp_path)
        assert capsys.readouterr().out == f'folder mapping: skipped: needs root to read {path}\n'
        assert path.stat().st_mode & 0o777 == 0
    finally:
        path.chmod(0o600)


@pytest.mark.parametrize('kind', ['missing', 'dangling', 'invalid'])
def test_dry_run_missing_primary_only_skips_fresh_install(tmp_path, capsys, kind):
    path = tmp_path / 'etc/default/accd'
    path.parent.mkdir(parents=True)
    if kind == 'dangling':
        path.symlink_to(tmp_path / 'missing')
        with pytest.raises(OSError):
            installer.dry_run(root=tmp_path)
    elif kind == 'invalid':
        path.mkdir()
        with pytest.raises(IsADirectoryError):
            installer.dry_run(root=tmp_path)
    else:
        installer.dry_run(root=tmp_path)
        assert capsys.readouterr().out == 'folder mapping: nothing installed yet, skipped\n'
