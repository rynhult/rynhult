"""Portable macOS plan contracts; these never invoke account or launchd tools."""
import importlib.util
import json
import plistlib
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]


def setup():
    source = ROOT / 'desktop/mac_setup.py'
    assert source.is_file(), 'macOS account installer missing'
    spec = importlib.util.spec_from_file_location('mac_setup', source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def owner():
    return SimpleNamespace(pw_uid=501, pw_gid=20, pw_name='alex', pw_dir='/Users/alex')


def test_dry_run_is_portable_and_has_no_effects(tmp_path):
    setup()
    result = subprocess.run([sys.executable, str(ROOT / 'desktop/mac_setup.py'), 'install',
        '--owner', 'alex', '--owner-uid', '501', '--owner-gid', '20', '--agent-id', '50001'],
        cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value['apply'] is False and value['owner_uid'] == 501
    assert not list(tmp_path.iterdir())
    commands = value['commands']
    assert ['/usr/bin/dscl', '.', '-create', '/Users/rynd-501', 'UserShell', '/usr/bin/false'] in commands
    assert ['/usr/bin/dscl', '.', '-create', '/Users/rynd-501', 'IsHidden', '1'] in commands
    assert ['/usr/bin/dscl', '.', '-create', '/Users/rynd-501', 'UniqueID', '50001'] in commands
    assert ['/usr/bin/dscl', '.', '-create', '/Users/rynd-501', 'PrimaryGroupID', '50001'] in commands
    assert ['/usr/bin/dscl', '.', '-create', '/Users/rynd-501', 'Password', '*'] in commands
    assert not any('admin' in arg or arg in ('sudo', 'brew', 'sysadminctl') for cmd in commands for arg in cmd)
    plist = plistlib.loads(next(v for k, v in value['files'].items() if k.endswith('.plist')).encode())
    assert plist['UserName'] == plist['GroupName'] == 'rynd-501'
    assert plist['ProgramArguments'][1] == '-I'
    assert plist['ProgramArguments'][-1] == '501'
    assert plist['AbandonProcessGroup'] is True
    sock = plist['Sockets']['Broker']
    assert sock['SockPathOwner'] == 501 and sock['SockPathGroup'] == 20
    assert sock['SockPathMode'] == 0o600 and len(sock['SockPathName'].encode()) < 104
    assert sock['SockType'] == 'stream' and sock['SockPassive'] is True
    manifest = json.loads(next(v for k, v in value['files'].items() if k.endswith('execution.json')))
    assert manifest['agent_uid'] == manifest['agent_gid'] == 50001
    assert manifest['owner_uid'] == 501
    assert manifest['home'].endswith('/Owners/501/rynd-501')
    assert any(cmd[:2] == ['/bin/chmod', '+a'] and 'file_inherit' in cmd[2] and 'directory_inherit' in cmd[2]
               and manifest['projects'] in cmd for cmd in commands)
    assert not any(cmd[:2] == ['/bin/chmod', '+a'] and manifest['home'] in cmd for cmd in commands)
    for folder in (manifest['home'], manifest['home'] + '/.local', manifest['home'] + '/.local/bin', manifest['projects'], manifest['run_dir'], manifest['run_dir'] + '/rynd-501'):
        reset = ['/bin/chmod', '-N', folder]
        assert reset in commands, 'private directories must discard inherited ACL grants'
        assert commands.index(reset) < next(i for i, cmd in enumerate(commands) if cmd[:2] == ['/bin/chmod', '+a'])
    assert ['/usr/bin/install', '-d', '-o', 'root', '-g', 'wheel', '-m', '0711', manifest['run_dir']] in commands
    assert not any(cmd[:6] == ['/usr/bin/install', '-d', '-o', 'rynd-501', '-g', 'rynd-501'] and manifest['run_dir'] in cmd for cmd in commands)


@pytest.mark.parametrize('uid,agent_id', [(0,50001), (500,50001), (501,501), (501,0), (501,65534)])
def test_plan_rejects_privileged_or_colliding_ids(uid, agent_id):
    module = setup()
    who = owner()
    who.pw_uid = uid
    with pytest.raises(ValueError):
        module.plan('install', who, agent_id)


def test_uninstall_preserves_by_default_and_purge_is_separate():
    module = setup()
    keep = module.plan('uninstall', owner(), 50001)
    assert not any('-delete' in cmd for cmd in keep['commands'])
    assert keep['purge'] is False
    purge = module.plan('uninstall', owner(), 50001, purge=True)
    assert purge['purge'] is True
    assert ['/usr/bin/dscl', '.', '-delete', '/Users/rynd-501'] in purge['commands']
    assert ['/usr/bin/dscl', '.', '-delete', '/Groups/rynd-501'] in purge['commands']
    assert all('/Users/alex' not in path for path in purge['remove'])
    assert purge['stop_tmux'] == ['tmux', '-L', 'rynhult-desktop-501', 'kill-server']


def test_apply_fails_closed_on_linux_before_any_commands(monkeypatch):
    module = setup()
    monkeypatch.setattr(module.sys, 'platform', 'linux')
    with pytest.raises(ValueError, match='macOS'):
        module.apply_plan(module.plan('install', owner(), 50001))


def test_acl_checker_rejects_write_grants_not_normal_delete_denials():
    module = setup()
    module.check_acl_output('drwxr-xr-x root wheel\n 0: group:everyone deny delete\n')
    with pytest.raises(ValueError, match='ACL'):
        module.check_acl_output('drwxr-xr-x root wheel\n 0: user:alex allow write,append,file_inherit\n')


def test_manifest_paths_and_numeric_identity_are_bound_to_owner():
    module = setup()
    value = module.plan('install', owner(), 50001)['identity']
    module.validate_manifest(value, 501)
    for change in ({'socket': '/tmp/agent.sock'}, {'home': '/Users/alex'}, {'owner_uid': 502},
                   {'agent_uid': 0}, {'agent_gid': 20}, {'resources': '/tmp/resources'}):
        with pytest.raises(ValueError):
            module.validate_manifest({**value, **change}, 501)


def test_main_composition_selects_mac_manifest_and_preserves_bootstrap(monkeypatch):
    module = setup()
    execution_path = ROOT / 'desktop/execution.py'
    spec = importlib.util.spec_from_file_location('desktop_mac_execution', execution_path)
    execution = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(execution)
    value = module.plan('install', owner(), 50001)['identity']
    monkeypatch.setattr(execution, 'sys', SimpleNamespace(platform='darwin'))
    monkeypatch.setattr(module, 'read_identity', lambda uid: value if uid == 501 else None)
    monkeypatch.setattr(execution, 'validate_account', lambda val, uid: None)
    assert execution.read_manifest(501) == value
    from acc.server.config import Settings
    settings = execution.execution_settings(Settings(), value)
    assert settings.default_account == 'rynd-501'
    assert str(settings.account_run_dir('rynd-501')).endswith('/Owners/501/run/rynd-501')
    assert str(settings.broker_socket).endswith('/Sockets/501/rynd-501.sock')


def test_directory_identity_checks_native_hidden_flag_guid_and_no_admin(monkeypatch):
    module = setup()
    value = module.plan('install', owner(), 50001)['identity']
    user = {'dsAttrTypeStandard:GeneratedUID':[value['agent_guid']],
            'dsAttrTypeStandard:UniqueID':['50001'], 'dsAttrTypeNative:IsHidden':['1']}
    group = {'dsAttrTypeStandard:GeneratedUID':[value['group_guid']], 'dsAttrTypeStandard:PrimaryGroupID':['50001']}
    account = SimpleNamespace(pw_name='rynd-501',pw_uid=50001,pw_gid=50001,pw_dir=value['home'],pw_shell='/usr/bin/false')
    monkeypatch.setattr(module,'record',lambda path: user if path.startswith('/Users/') else group)
    monkeypatch.setattr(module,'pwd',SimpleNamespace(getpwnam=lambda name:account))
    monkeypatch.setattr(module,'grp',SimpleNamespace(getgrnam=lambda name:SimpleNamespace(gr_gid=80)))
    monkeypatch.setattr(module,'os',SimpleNamespace(getgrouplist=lambda name,gid:[50001]))
    monkeypatch.setattr(module,'check_groups',lambda uid:None)
    module.verify_identity(value)
    user['dsAttrTypeNative:IsHidden'] = ['0']
    with pytest.raises(ValueError,match='hidden'):
        module.verify_identity(value)
    user['dsAttrTypeNative:IsHidden'] = ['1']
    monkeypatch.setattr(module,'os',SimpleNamespace(getgrouplist=lambda name,gid:[50001,80]))
    with pytest.raises(ValueError,match='admin'):
        module.verify_identity(value)


def test_first_run_can_open_before_installer_resources_exist(monkeypatch):
    import builtins
    spec = importlib.util.spec_from_file_location('desktop_mac_first_run',ROOT/'desktop/execution.py')
    execution = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(execution)
    monkeypatch.setattr(execution,'sys',SimpleNamespace(platform='darwin'))
    original = builtins.__import__
    def no_mac_setup(name,*args,**kwargs):
        if name == 'mac_setup':
            raise ModuleNotFoundError('mac_setup',name='mac_setup')
        return original(name,*args,**kwargs)
    monkeypatch.setattr(builtins,'__import__',no_mac_setup)
    monkeypatch.setattr(execution,'Path',lambda path:SimpleNamespace(exists=lambda:False))
    assert execution.read_manifest(501) is None


def test_acl_checker_fails_closed_on_unknown_allow_permissions():
    with pytest.raises(ValueError,match='ACL'):
        setup().check_acl_output('drwxr-xr-x root wheel\n 0: user:alex allow generic_all\n')


def test_purge_waits_for_owned_process_exit_without_pattern_kills(monkeypatch):
    module = setup()
    outputs = iter(['50001 123\n501 999\n', ''])
    monkeypatch.setattr(module.subprocess,'run',lambda *a,**kw:SimpleNamespace(stdout=next(outputs)))
    module.wait_for_agent_exit(50001,timeout=1)
    monkeypatch.setattr(module.subprocess,'run',lambda *a,**kw:SimpleNamespace(stdout='50001 123\n'))
    with pytest.raises(ValueError,match='123'):
        module.wait_for_agent_exit(50001,timeout=0)


def test_uninstall_refuses_explicit_agent_id_that_disagrees_with_receipt(monkeypatch):
    module = setup()
    value = module.plan('install',owner(),50001)['identity']
    monkeypatch.setattr(module,'sys',SimpleNamespace(platform='darwin'))
    monkeypatch.setattr(module,'read_identity',lambda *a,**kw:value)
    monkeypatch.setattr(sys,'argv',['mac_setup.py','uninstall','--owner','alex','--owner-uid','501',
                                  '--owner-gid','20','--agent-id','50002'])
    with pytest.raises(SystemExit) as stopped:
        module.main()
    assert stopped.value.code == 1


def test_hidden_agent_is_refused_by_remote_login_even_with_its_own_key():
    # Password '*' and a false shell do not stop OpenSSH public-key logins or -N port forwarding.
    module = setup()
    install = module.plan('install', owner(), 50001)
    denial = install['files'][module.SSHD_DROPINS + '/100-rynhult-desktop-501.conf']
    assert [line for line in denial.splitlines() if not line.startswith('#')] == ['DenyUsers rynd-501']
    # A plain uninstall keeps the account, so it keeps the denial; only purge removes it with the account.
    assert not any('sshd' in path for path in module.plan('uninstall', owner(), 50001)['remove'])
    assert module.SSHD_DROPINS + '/100-rynhult-desktop-501.conf' in module.plan('uninstall', owner(), 50001, purge=True)['remove']


@pytest.mark.parametrize('config,ok', [
    ('# macOS default\nInclude /etc/ssh/sshd_config.d/*\nPort 22\n', True),
    ('Port 22\n#Include /etc/ssh/sshd_config.d/*\n', False),
    ('Port 22\n', False)])
def test_install_requires_sshd_to_read_the_denial(config, ok):
    module = setup()
    if ok:
        module.check_sshd_includes(config)
    else:
        with pytest.raises(ValueError, match='Remote Login'):
            module.check_sshd_includes(config)


def fake_darwin_apply(monkeypatch, module, value):
    """Run apply_plan's logic on Linux with every system call recorded, never executed."""
    calls = []
    def run(argv, **kw):
        calls.append((list(argv), kw.get('user')))
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    monkeypatch.setattr(module, 'sys', SimpleNamespace(platform='darwin'))
    monkeypatch.setattr(module.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(module, 'RESOURCES', ROOT)
    monkeypatch.setattr(module, 'protected_resources', lambda: None)
    monkeypatch.setattr(module, 'protected', lambda paths: None)
    monkeypatch.setattr(module, 'read_identity', lambda uid, active=True: value['identity'])
    monkeypatch.setattr(module, 'verify_identity', lambda info, groups=True: None)
    monkeypatch.setattr(module.pwd, 'getpwnam', lambda name: owner())
    monkeypatch.setattr(module.subprocess, 'run', run)
    monkeypatch.setattr(module, 'wait_for_agent_exit', lambda uid, timeout=5: calls.append((['wait', uid], None)))
    return calls


def test_uninstall_ends_every_agent_process_and_its_crontab_not_just_the_broker(monkeypatch):
    # AbandonProcessGroup keeps the tmux server (and anything the agent started) alive after bootout.
    module = setup()
    monkeypatch.setattr(module, 'RESOURCES', ROOT)  # apply_plan only runs from its installed copy
    value = module.plan('uninstall', owner(), 50001)
    calls = fake_darwin_apply(monkeypatch, module, value)
    module.apply_plan(value)
    argv = [call for call, _ in calls]
    assert argv[0] == ['/bin/launchctl', 'bootout', 'system/com.rynhult.desktop.501']
    tmux = argv.index(['tmux', '-L', 'rynhult-desktop-501', 'kill-server'])
    assert calls[tmux][1] == 50001, 'tmux is stopped as the agent, never as root'
    for command in (['/bin/launchctl', 'bootout', 'user/50001'], ['/usr/bin/crontab', '-r', '-u', 'rynd-501'],
                    ['/usr/bin/pkill', '-KILL', '-U', '50001']):
        assert tmux < argv.index(command) < argv.index(['wait', 50001])
    assert not any('-delete' in call for call in argv), 'a plain uninstall keeps the account'


@pytest.mark.parametrize('answers,error', [
    ({}, None),
    ({20: 'user is a member of the group\n'}, 'staff'),  # e.g. through nested or calculated groups
    ({80: 'user is a member of the group\n'}, 'admin'),
    ({20: 'unexpected\n'}, 'membership')])
def test_agent_shares_no_group_with_owner_homes_or_administrators(monkeypatch, answers, error):
    # Owner homes are 0750 with group staff: membership there, however it arises, reads the owner's files.
    module = setup()
    calls = []
    def run(argv, **kw):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=answers.get(int(argv[-1]), 'user is not a member of the group\n'))
    monkeypatch.setattr(module.subprocess, 'run', run)
    if error is None:
        module.check_groups(50001)
    else:
        with pytest.raises(ValueError, match=error):
            module.check_groups(50001)
    assert calls[0][:3] == ['/usr/bin/dsmemberutil', 'checkmembership', '-u']


def test_an_account_refused_for_its_groups_can_still_be_purged(monkeypatch):
    # The membership check runs right after dscl creates the account: a refusal there must stay recoverable.
    module = setup()
    monkeypatch.setattr(module, 'RESOURCES', ROOT)
    value = module.plan('uninstall', owner(), 50001, purge=True)
    calls = fake_darwin_apply(monkeypatch, module, value)
    seen = []
    monkeypatch.setattr(module, 'verify_identity', lambda info, groups=True: seen.append(groups))
    monkeypatch.setattr(module.pwd, 'getpwall', lambda: [])
    rmtree = lambda path: calls.append((['rmtree', str(path)], None))
    rmtree.avoids_symlink_attacks = True
    monkeypatch.setattr(module.shutil, 'rmtree', rmtree)
    monkeypatch.setattr(module.Path, 'rmdir', lambda self: None)
    module.apply_plan(value)
    assert seen == [False]
    assert ['/usr/bin/dscl', '.', '-delete', '/Users/rynd-501'] in [c for c, _ in calls]
