"""Setup plans must never perform privileged effects without explicit apply."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Linux systemd/account setup only')

ROOT = Path(__file__).resolve().parents[2]


def load_setup():
    path = ROOT / 'desktop/setup.py'
    assert path.exists(), 'desktop account setup is missing'
    spec = importlib.util.spec_from_file_location('desktop_setup', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_setup_dry_run_creates_nothing(tmp_path):
    load_setup()
    result = subprocess.run([sys.executable, str(ROOT / 'desktop/setup.py'), 'install',
                             '--owner', __import__('pwd').getpwuid(__import__('os').getuid()).pw_name],
                            capture_output=True, text=True, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan['apply'] is False
    assert not list(tmp_path.iterdir())
    assert plan['agent'] != plan['owner']
    assert any(cmd[0] == 'useradd' for cmd in plan['commands'])
    assert not any('sudo' in cmd or 'sudoers' in str(cmd) for cmd in plan['commands'])
    service = next(v for k, v in plan['files'].items() if k.endswith('.service'))
    assert f"User={plan['agent']}\n" in service
    assert 'NoNewPrivileges=yes' in service
    socket = next(v for k, v in plan['files'].items() if k.endswith('.socket'))
    assert 'SocketUser=' in socket and 'SocketMode=0600' in socket
    manifest = json.loads(next(v for k, v in plan['files'].items() if k.endswith('execution.json')))
    assert manifest['owner_uid'] == __import__('os').getuid()
    assert manifest['agent'] == plan['agent']
    assert manifest['resources'] == str(ROOT)
    prefix = str(Path(manifest['home']) / '.local')
    assert any(cmd[0] == 'install' and prefix in cmd and '-o' in cmd for cmd in plan['commands'])
    spool = str(Path(manifest['run_dir']) / manifest['agent'])
    assert any(cmd[0] == 'install' and spool in cmd for cmd in plan['commands'])
    assert any(cmd[0] == 'setfacl' and spool in cmd for cmd in plan['commands'])


def test_uninstall_preserves_all_work_and_account():
    module = load_setup()
    plan = module.plan('uninstall', __import__('pwd').getpwuid(__import__('os').getuid()))
    assert not any(cmd[0] in {'userdel', 'groupdel', 'rm'} for cmd in plan['commands'])
    uid = owner().pw_uid
    assert set(plan['remove']) == {f'/etc/rynhult-desktop/{uid}/execution.json',
                                  f'/etc/systemd/system/rynhult-desktop-{uid}.socket',
                                  f'/etc/systemd/system/rynhult-desktop-{uid}.service',
                                  f'/run/rynhult-desktop/{uid}/{plan["agent"]}.sock'}
    assert not any('/var/lib/' in p for p in plan['remove'])


def test_apply_refuses_untrusted_resources_before_commands(tmp_path):
    module = load_setup()
    tmp_path.chmod(0o777)
    with pytest.raises(ValueError, match='root-owned'):
        module.trusted_tree(tmp_path)


def owner():
    import os
    import pwd
    return pwd.getpwuid(os.getuid())


def test_agent_account_cannot_log_in_outside_its_service():
    """A locked password still allows SSH public-key login (UsePAM) and cron/at outside NoNewPrivileges.
    An expired account refuses every PAM login while systemd's User= still runs the broker."""
    plan = load_setup().plan('install', owner())
    useradd = next(cmd for cmd in plan['commands'] if cmd[0] == 'useradd')
    assert '--expiredate' in useradd and useradd[useradd.index('--expiredate') + 1] == '1970-01-02'


def test_service_cannot_read_owner_home_and_socket_belongs_to_owner_only():
    plan = load_setup().plan('install', owner())
    service = next(v for k, v in plan['files'].items() if k.endswith('.service'))
    assert 'ProtectHome=yes\n' in service  # the agent home lives under /var/lib, never /home
    socket = next(v for k, v in plan['files'].items() if k.endswith('.socket'))
    # Not the owner's primary group: on shared-group systems ("users") every member could connect.
    assert f"SocketUser={owner().pw_uid}\n" in socket and 'SocketMode=0600\n' in socket
    assert 'SocketGroup' not in socket


def test_run_root_stays_root_owned_like_the_server_layout():
    plan = load_setup().plan('install', owner())
    run = str(plan['base']) + '/run'
    owned = [cmd for cmd in plan['commands'] if cmd[0] == 'install' and '-o' in cmd]
    assert all(run not in cmd for cmd in owned)
    assert any(cmd[0] == 'install' and run in cmd and '-o' not in cmd for cmd in plan['commands'])
    acl = next(cmd for cmd in plan['commands'] if cmd[0] == 'setfacl')
    assert run not in acl


def fake_root(module, monkeypatch, tmp_path, *, verified_uninstall=True):
    calls = []
    monkeypatch.setattr(module.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(module, 'trusted_tree', lambda path: None)
    monkeypatch.setattr(module, 'validate_destinations', lambda value: None)
    if verified_uninstall:
        monkeypatch.setattr(module, 'validate_uninstall', lambda value: 2000)
    monkeypatch.setattr(module, 'wait_for_uid_exit', lambda uid: None)
    monkeypatch.setattr(module.shutil, 'which', lambda name: f'/usr/bin/{name}')

    def run(command, check, **kwargs):
        calls.append(command)
        if command[:2] == ['systemctl', 'show']:
            return subprocess.CompletedProcess(command, 0, stdout='LoadState=not-found\nActiveState=inactive\nUnitFileState=\n')
        failing = {'pkill', 'crontab'} | ({'systemctl'} if command[1:2] == ['stop'] else set())
        if command[0] in failing:  # nothing to kill, no crontab, unit never loaded
            if check:
                raise subprocess.CalledProcessError(1, command)
            return subprocess.CompletedProcess(command, 1, stderr=f'no crontab for {command[-1]}\n' if command[0] == 'crontab' else '')
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(module.subprocess, 'run', run)
    return calls


def test_install_refuses_leftover_agent_data_before_any_command(monkeypatch, tmp_path):
    """Data from an earlier agent (or planted by whoever later received its UID) is never adopted."""
    module = load_setup()
    calls = fake_root(module, monkeypatch, tmp_path)
    monkeypatch.setattr(module.pwd, 'getpwnam', lambda name: (_ for _ in ()).throw(KeyError(name)))
    value = module.plan('install', owner())
    value['files'] = {str(tmp_path / 'absent.json'): ''}
    value['base'] = str(tmp_path)  # exists
    with pytest.raises(ValueError, match='already exists'):
        module.apply_plan(value)
    assert calls == []


def test_uninstall_disables_the_agent_and_tolerates_partial_state(monkeypatch, tmp_path):
    """KillMode=process leaves tmux and agent work running after the units go; uninstall must stop it and
    block every later login or scheduled job, while keeping the account and data for recovery."""
    module = load_setup()
    calls = fake_root(module, monkeypatch, tmp_path)
    value = module.plan('uninstall', owner())
    agent = value['agent']
    flat = [cmd for cmd in value['commands'] + value['cleanup']]
    assert ['usermod', '--lock', '--expiredate', '1970-01-02', agent] in flat
    assert ['crontab', '-r', '-u', agent] in flat
    assert ['pkill', '-KILL', '-u', agent] in flat
    value['remove'] = []
    module.apply_plan(value)  # verified-absent unit/process/crontab is an acceptable no-op
    assert ['systemctl', 'daemon-reload'] in calls
    assert calls.index(['usermod', '--lock', '--expiredate', '1970-01-02', agent]) < calls.index(['pkill', '-KILL', '-u', agent])


def test_owner_can_open_trusted_run_root_readonly():
    from types import SimpleNamespace

    person = SimpleNamespace(pw_uid=1000, pw_name='desktop-alex')
    plan = load_setup().plan('install', person)
    run = str(Path(plan['base']) / 'run')
    # Ingest opens this root with O_RDONLY|O_DIRECTORY before walking descendants.
    # Traverse-only 0711 permits ordinary path reads but that descriptor open fails.
    assert ['setfacl', '-m', 'u:1000:r-x', run] in plan['commands']
    assert not any(cmd[0] == 'setfacl' and run in cmd and ('rwx' in ' '.join(cmd) or 'd:' in ' '.join(cmd))
                   for cmd in plan['commands'])
    assert all(run not in cmd for cmd in plan['commands'] if cmd[0] == 'install' and '-o' in cmd)


def test_uninstall_removes_only_the_owners_stopped_runtime_socket():
    plan = load_setup().plan('uninstall', owner())
    socket = f'/run/rynhult-desktop/{owner().pw_uid}/{plan["agent"]}.sock'
    assert socket in plan['remove']
    assert [path for path in plan['remove'] if path.startswith('/run/')] == [socket]
    assert ['systemctl', 'disable', '--now', f'rynhult-desktop-{owner().pw_uid}.socket'] in plan['cleanup']
    assert ['systemctl', 'stop', f'rynhult-desktop-{owner().pw_uid}.service'] in plan['cleanup']


@pytest.mark.parametrize('action', ['install', 'uninstall'])
def test_apply_refuses_symlinked_destination_parent_before_effects(monkeypatch, tmp_path, action):
    module = load_setup()
    validate = module.validate_destinations
    calls = fake_root(module, monkeypatch, tmp_path)
    monkeypatch.setattr(module, 'validate_destinations', validate)
    monkeypatch.setattr(module.pwd, 'getpwnam', lambda name: (_ for _ in ()).throw(KeyError(name)))
    resources = tmp_path / 'resources'
    for name in ('desktop/runtime/python/bin/python3', 'desktop/broker.py', 'bin/acc-hook', 'bin/acc-guard'):
        path = resources / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('fixture')
    monkeypatch.setattr(module, 'RESOURCES', resources)
    outside = tmp_path / 'outside'
    outside.mkdir()
    alias = tmp_path / 'config'
    alias.symlink_to(outside, target_is_directory=True)
    victim = outside / 'execution.json'
    if action == 'uninstall':
        victim.write_text('unrelated file')
    value = module.plan(action, owner())
    value.update(base=str(tmp_path / 'new-data'), commands=[], cleanup=[], finish=[],
                 files={str(alias / 'execution.json'): 'new configuration'} if action == 'install' else {},
                 remove=[str(alias / 'execution.json')] if action == 'uninstall' else [])
    with pytest.raises(ValueError, match='destination'):
        module.apply_plan(value)
    assert calls == []
    assert victim.read_text() == 'unrelated file' if action == 'uninstall' else not victim.exists()


@pytest.mark.parametrize('uid,mode', [(1000, 0o755), (0, 0o777)])
def test_destination_metadata_must_be_root_owned_and_protected(monkeypatch, tmp_path, uid, mode):
    import stat
    from types import SimpleNamespace

    module = load_setup()
    parent = tmp_path / 'destination'
    original = Path.lstat
    monkeypatch.setattr(Path, 'lstat', lambda self: SimpleNamespace(st_uid=uid, st_mode=stat.S_IFDIR | mode)
                        if self == parent else original(self))
    value = module.plan('install', owner())
    value['files'] = {str(parent / 'execution.json'): ''}
    with pytest.raises(ValueError, match='destination'):
        module.validate_destinations(value)


def test_uninstall_requires_a_provisioning_record_before_locking_an_account(monkeypatch, tmp_path):
    module = load_setup()
    calls = fake_root(module, monkeypatch, tmp_path, verified_uninstall=False)
    value = module.plan('uninstall', owner())
    value['remove'] = []
    # No record belonging to this desktop installation; the generated agent name
    # could belong to an unrelated pre-existing account.
    value['manifest'] = str(tmp_path / 'absent-execution.json')
    with pytest.raises(ValueError, match='provisioning'):
        module.apply_plan(value)
    assert calls == []


@pytest.mark.parametrize('bad', ['owner', 'home', 'root-account', 'owner-account', 'missing-account'])
def test_uninstall_verifies_manifest_and_account_binding(monkeypatch, bad):
    from types import SimpleNamespace

    module = load_setup()
    person = SimpleNamespace(pw_uid=1000, pw_name='alex')
    value = module.plan('uninstall', person)
    saved = json.loads(next(v for k, v in module.plan('install', person)['files'].items()
                            if k.endswith('execution.json')))
    account = SimpleNamespace(pw_uid=2000, pw_dir=saved['home'])
    if bad == 'owner':
        saved['owner_uid'] = 1001
    elif bad == 'home':
        account.pw_dir = '/unrelated-home'
    elif bad == 'root-account':
        account.pw_uid = 0
    elif bad == 'owner-account':
        account.pw_uid = 1000
    monkeypatch.setattr(module, 'read_provisioning', lambda path: saved)
    monkeypatch.setattr(module.pwd, 'getpwnam', lambda name: account)
    if bad == 'missing-account':
        monkeypatch.setattr(module.pwd, 'getpwnam', lambda name: (_ for _ in ()).throw(KeyError(name)))
    with pytest.raises(ValueError, match='provision'):
        module.validate_uninstall(value)


def test_uninstall_accepts_its_own_protected_binding(monkeypatch):
    from types import SimpleNamespace

    module = load_setup()
    person = SimpleNamespace(pw_uid=1000, pw_name='alex')
    saved = json.loads(next(v for k, v in module.plan('install', person)['files'].items()
                            if k.endswith('execution.json')))
    monkeypatch.setattr(module, 'read_provisioning', lambda path: saved)
    monkeypatch.setattr(module.pwd, 'getpwnam', lambda name: SimpleNamespace(pw_uid=2000, pw_dir=saved['home']))
    module.validate_uninstall(module.plan('uninstall', person))


def test_provisioning_reader_does_not_follow_a_final_symlink(tmp_path):
    path = tmp_path / 'execution.json'
    target = tmp_path / 'elsewhere'
    target.write_text('{}')
    path.symlink_to(target)
    with pytest.raises(ValueError, match='provisioning'):
        load_setup().read_provisioning(str(path))


@pytest.mark.parametrize('returncode', [2, 3])
def test_uninstall_keeps_recovery_record_if_uid_cleanup_fails(monkeypatch, tmp_path, returncode):
    module = load_setup()
    calls = fake_root(module, monkeypatch, tmp_path)
    previous = module.subprocess.run
    record = tmp_path / 'execution.json'
    record.write_text('retain for retry')
    value = module.plan('uninstall', owner())
    value['remove'] = [str(record)]

    def run(command, check, **kwargs):
        if command[0] == 'pkill':
            return subprocess.CompletedProcess(command, returncode)
        return previous(command, check, **kwargs)

    monkeypatch.setattr(module.subprocess, 'run', run)
    with pytest.raises(ValueError, match='cleanup'):
        module.apply_plan(value)
    assert record.read_text() == 'retain for retry'
    assert ['systemctl', 'daemon-reload'] not in calls


@pytest.mark.parametrize('failure', ['service-active', 'cron-error'])
def test_uninstall_refuses_unconfirmed_service_or_cron_cleanup(monkeypatch, tmp_path, failure):
    module = load_setup()
    calls = fake_root(module, monkeypatch, tmp_path)
    previous = module.subprocess.run
    record = tmp_path / 'execution.json'
    record.write_text('retain')
    value = module.plan('uninstall', owner())
    value['remove'] = [str(record)]

    def run(command, check, **kwargs):
        if failure == 'service-active' and command[:2] == ['systemctl', 'show']:
            return subprocess.CompletedProcess(command, 0, stdout='LoadState=loaded\nActiveState=active\nUnitFileState=enabled\n')
        if failure == 'cron-error' and command[0] == 'crontab':
            return subprocess.CompletedProcess(command, 1, stderr='permission denied')
        return previous(command, check, **kwargs)

    monkeypatch.setattr(module.subprocess, 'run', run)
    with pytest.raises(ValueError, match='cleanup'):
        module.apply_plan(value)
    assert record.read_text() == 'retain'
    assert ['systemctl', 'daemon-reload'] not in calls


@pytest.mark.parametrize('returncode', [0, 1])
def test_uninstall_cannot_infer_uid_exit_from_pkill_status(monkeypatch, tmp_path, returncode):
    module = load_setup()
    fake_root(module, monkeypatch, tmp_path)
    previous = module.subprocess.run
    record = tmp_path / 'execution.json'
    record.write_text('retain')
    value = module.plan('uninstall', owner())
    value['remove'] = [str(record)]

    def still_running(uid):
        raise ValueError('Desktop cleanup could not confirm agent exit')

    monkeypatch.setattr(module, 'wait_for_uid_exit', still_running, raising=False)

    def run(command, check, **kwargs):
        if command[0] == 'pkill':
            return subprocess.CompletedProcess(command, returncode)
        return previous(command, check, **kwargs)

    monkeypatch.setattr(module.subprocess, 'run', run)
    with pytest.raises(ValueError, match='cleanup'):
        module.apply_plan(value)
    assert record.read_text() == 'retain'


def test_uid_scan_detects_real_effective_saved_and_filesystem_identity(tmp_path):
    module = load_setup()
    task = tmp_path / '123'
    task.mkdir()
    status = task / 'status'
    for ids in ('2000 2001 2002 2003', '2001 2000 2002 2003', '2001 2002 2000 2003', '2001 2002 2003 2000'):
        status.write_text(f'Uid:\t{ids}\nState:\tS (sleeping)\n')
        assert module.active_uid(2000, tmp_path)
    status.write_text('Uid:\t2000 2000 2000 2000\nState:\tZ (zombie)\n')
    assert not module.active_uid(2000, tmp_path)
    status.write_text('Uid:\t3000 3000 3000 3000\nState:\tS (sleeping)\n')
    assert not module.active_uid(2000, tmp_path)


def test_uid_wait_is_bounded_and_refuses_unconfirmed_exit(monkeypatch):
    module = load_setup()
    clock = [0.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(module.time, 'sleep', lambda delay: clock.__setitem__(0, clock[0] + delay))
    monkeypatch.setattr(module, 'active_uid', lambda uid: True)
    with pytest.raises(ValueError, match='cleanup'):
        module.wait_for_uid_exit(2000, timeout=0.2)
    assert clock[0] == 0.2
    monkeypatch.setattr(module, 'active_uid', lambda uid: False)
    module.wait_for_uid_exit(2000)


def test_uid_scan_refuses_incomplete_process_status(tmp_path):
    task = tmp_path / '123'
    task.mkdir()
    (task / 'status').write_text('Name: fixture\n')
    with pytest.raises(ValueError, match='cleanup'):
        load_setup().active_uid(2000, tmp_path)


@pytest.mark.parametrize('missing', ['pkill', 'crontab'])
def test_install_requires_tools_needed_for_safe_uninstall(monkeypatch, tmp_path, missing):
    module = load_setup()
    calls = fake_root(module, monkeypatch, tmp_path)
    monkeypatch.setattr(module.shutil, 'which', lambda name: None if name == missing else '/usr/bin/' + name)
    monkeypatch.setattr(module.pwd, 'getpwnam', lambda name: (_ for _ in ()).throw(KeyError(name)))
    resources = tmp_path / 'resources'
    for name in ('desktop/runtime/python/bin/python3', 'desktop/broker.py', 'bin/acc-hook', 'bin/acc-guard'):
        path = resources / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('fixture')
    monkeypatch.setattr(module, 'RESOURCES', resources)
    value = module.plan('install', owner())
    value.update(base=str(tmp_path / 'new-data'), files={})
    with pytest.raises(ValueError, match=missing):
        module.apply_plan(value)
    assert calls == []
