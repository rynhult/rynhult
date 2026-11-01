"""Fixed root-operation adapter: no commands run on the test host."""
import pytest

from acc.folder_policy import FolderError


def test_purpose_group_collision_and_untrusted_account_refuse_before_commands(monkeypatch):
    from acc.folder_host import LinuxFolderHost
    host = LinuxFolderHost()
    monkeypatch.setattr(host, '_groups', lambda: [('ryn-repo-' + 'a' * 16, 62345, [])])
    account = {'uid': 12345, 'owner_uid': 12346, 'agent_name': 'alex-agent', 'owner_name': 'alex'}
    with pytest.raises(FolderError, match='already exists'):
        host.plan_group('a' * 32, account)
    with pytest.raises(FolderError):
        host.plan_group('b' * 32, account | {'owner_name': '--root'})


def test_membership_partial_failure_retains_only_known_changes(monkeypatch):
    from acc.folder_host import LinuxFolderHost
    host = LinuxFolderHost()
    account = {'uid': 12345, 'owner_uid': 12346, 'agent_name': 'alex-agent', 'owner_name': 'alex'}
    monkeypatch.setattr(host, '_groups', lambda: [])
    group = host.plan_group('b' * 32, account)
    journal, calls = [], []
    def run(argv):
        calls.append(argv)
        if argv[0] == '/usr/sbin/usermod' and argv[-1] == 'alex-agent':
            raise FolderError('Membership update failed.')
        return ''
    monkeypatch.setattr(host, '_run', run)
    with pytest.raises(FolderError):
        host.create_group(group, account, lambda: journal.append(dict(group)))
    assert group['created'] is True
    assert group['members_added'] == ['alex']
    assert calls[0] == ['/usr/sbin/groupadd', '--gid', str(group['gid']), '--', group['name']]
    # A failed/unconfirmed membership is not automatically removed as if owned.
    assert group['membership_pending'] == 'alex-agent'
    assert journal


def test_active_agent_process_refuses_without_stopping_service(monkeypatch):
    from acc.folder_host import LinuxFolderHost
    host = LinuxFolderHost()
    monkeypatch.setattr(host, '_processes', lambda uid: {101, 102})
    calls = []
    monkeypatch.setattr(host, '_run', lambda argv: calls.append(argv) or '101')
    account = {'uid': 12345, 'agent_name': 'alex-agent', 'broker_unit': 'acc-broker@alex-agent.service',
        'broker_socket_unit': 'acc-broker@alex-agent.socket'}
    with pytest.raises(FolderError, match='processes'):
        with host.stopped(account):
            pytest.fail('Active account must not reach permission mutation.')
    assert not any('stop' in argv for argv in calls)


def test_restart_failure_is_not_reported_as_a_successful_grant(monkeypatch):
    from acc.folder_host import LinuxFolderHost
    host = LinuxFolderHost()
    monkeypatch.setattr(host, '_processes', lambda uid: set())
    calls = []
    def run(argv):
        calls.append(argv)
        if 'start' in argv:
            raise FolderError('Account service restart needs repair.')
        return '0'
    monkeypatch.setattr(host, '_run', run)
    with pytest.raises(FolderError, match='restart'):
        with host.stopped({'uid': 12345, 'agent_name': 'alex-agent', 'broker_unit': 'acc-broker@alex-agent.service',
                           'broker_socket_unit': 'acc-broker@alex-agent.socket'}):
            pass
    assert any('start' in argv for argv in calls)
