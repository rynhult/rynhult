"""Classify argv only: these regressions never execute an account-management tool."""
import pytest
from host_isolation import HostAccessError, HostGuard

HUMAN = 'accqh0123abcd'
AGENT = 'accqa0123abcd'
GROUP = 'ryn-repo-0123456789abcdef'


def useradd(name=HUMAN, uid='61000'):
    return ['/usr/sbin/useradd', '--no-create-home', '--no-user-group', '--uid', uid,
        '--gid', '65534', '--shell', '/usr/sbin/nologin', '--', name]


def groupadd():
    return ['/usr/sbin/groupadd', '--gid', '60000', '--', GROUP]


def authorize(guard):
    for argv in [useradd(), useradd(AGENT, '61001'), groupadd()]:
        guard.command(argv[0], argv, {})


def test_fixed_fixture_lifecycle_is_allowed_only_by_root_vm_guard(monkeypatch):
    monkeypatch.setattr('host_isolation.os.geteuid', lambda: 0)
    guard = HostGuard(disposable_root_vm=True)
    authorize(guard)
    for argv in [
        ['/usr/sbin/usermod', '--append', '--groups', GROUP, '--', HUMAN],
        ['/usr/sbin/usermod', '--append', '--groups', GROUP, '--', AGENT],
        ['/usr/bin/gpasswd', '--delete', AGENT, GROUP],
        ['/usr/sbin/gpasswd', '--delete', HUMAN, GROUP],
        ['/usr/sbin/groupdel', '--', GROUP],
        ['/usr/sbin/userdel', '--', AGENT],
        ['/usr/sbin/userdel', '--', HUMAN],
    ]:
        guard.command(argv[0], argv, {})
    assert not guard.violations


@pytest.mark.parametrize(('uid','opted_in'), [(0, False), (1004, True), (1004, False)])
def test_fixture_creation_stays_blocked_without_both_root_and_captured_optin(monkeypatch, uid, opted_in):
    monkeypatch.setattr('host_isolation.os.geteuid', lambda: uid)
    guard = HostGuard(disposable_root_vm=opted_in)
    argv = useradd()
    with pytest.raises(HostAccessError):
        guard.command(argv[0], argv, {})


@pytest.mark.parametrize('argv', [
    useradd('root'), useradd('--evil'), useradd(uid='0'), useradd(uid='061000'),
    useradd(uid='62000'), useradd() + ['--password', 'never'],
    ['/usr/sbin/useradd', HUMAN],
    ['/usr/sbin/userdel', '--remove', HUMAN],
    ['/usr/sbin/userdel', '--', 'accqhdeadbeef'],
    ['/usr/sbin/groupadd', '--gid', '0', '--', GROUP],
    ['/usr/sbin/groupadd', '--force', '--gid', '60000', '--', GROUP],
    ['/usr/sbin/groupadd', '--gid', '65000', '--', GROUP],
    ['/usr/sbin/groupdel', '--', 'sudo'],
    ['/usr/sbin/groupdel', '--', 'ryn-repo-deadbeefdeadbeef'],
    ['/usr/sbin/usermod', '--append', '--groups', 'sudo', '--', AGENT],
    ['/usr/sbin/usermod', '--append', '--groups', GROUP + ',docker', '--', AGENT],
    ['/usr/sbin/usermod', '--groups', GROUP, '--', AGENT],
    ['/usr/sbin/usermod', '--append', '--groups', GROUP, '--', 'root'],
    ['/usr/bin/gpasswd', '--delete', AGENT, 'sudo'],
    ['/usr/bin/gpasswd', '--add', AGENT, GROUP],
    ['/usr/bin/gpasswd', '--delete', 'root', GROUP],
    ['/usr/sbin/useradd', '--root', '/tmp', HUMAN],
    ['/usr/bin/../sbin/useradd', *useradd()[1:]],
])
def test_root_optin_never_authorizes_other_arguments_or_existing_identities(monkeypatch, argv):
    monkeypatch.setattr('host_isolation.os.geteuid', lambda: 0)
    guard = HostGuard(disposable_root_vm=True)
    authorize(guard)
    with pytest.raises(HostAccessError):
        guard.command(argv[0], argv, {})


def test_shell_text_and_executable_argv_mismatch_are_not_typed_qualification_requests(monkeypatch):
    monkeypatch.setattr('host_isolation.os.geteuid', lambda: 0)
    guard = HostGuard(disposable_root_vm=True)
    argv = useradd()
    with pytest.raises(HostAccessError):
        guard.command(argv[0], ' '.join(argv), {})
    with pytest.raises(HostAccessError):
        guard.command(argv[0], ['useradd', *argv[1:]], {})


def test_gpasswd_is_not_an_unguarded_host_control_command():
    with pytest.raises(HostAccessError):
        HostGuard().command('/usr/bin/gpasswd', ['/usr/bin/gpasswd', '--delete', 'root', 'sudo'], {})


def test_environment_flag_cannot_enable_an_existing_guard(monkeypatch):
    monkeypatch.setattr('host_isolation.os.geteuid', lambda: 0)
    monkeypatch.setenv('ACC_FOLDER_QUALIFICATION_DISPOSABLE', '1')
    argv = useradd()
    with pytest.raises(HostAccessError):
        HostGuard().command(argv[0], argv, {})


def test_other_host_control_stays_blocked_even_in_a_disposable_root_vm(monkeypatch):
    monkeypatch.setattr('host_isolation.os.geteuid', lambda: 0)
    guard = HostGuard(disposable_root_vm=True)
    for argv in [['/usr/bin/systemctl', 'restart', 'accd'], ['/usr/bin/sudo', 'true'],
                 ['/usr/sbin/loginctl', 'enable-linger', HUMAN]]:
        with pytest.raises(HostAccessError):
            guard.command(argv[0], argv, {})


@pytest.mark.parametrize(('created','name','expected'), [
    (True, GROUP, GROUP), (False, GROUP, None), ('true', GROUP, None), (True, 'sudo', None),
])
def test_cleanup_recovers_only_confirmed_own_group_after_failed_apply(tmp_path, created, name, expected):
    import json

    from test_folder_group_root_qualification import confirmed_group
    (tmp_path / 'state').mkdir()
    operation = '0123456789abcdef' * 2
    (tmp_path / 'state' / (operation + '.json')).write_text(json.dumps({
        'request': {'operation_id': operation}, 'group': {'created': created, 'name': name}}))
    assert confirmed_group(tmp_path, operation) == expected
