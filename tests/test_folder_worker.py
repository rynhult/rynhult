"""The uninstalled worker uses only provisioned mappings and reviewed inode plans."""
import os

import pytest


@pytest.fixture
def worker(tmp_path):
    from folder_host_fixture import FixtureHost

    from acc.folder_worker import FolderWorker
    root = tmp_path / 'host'
    repo = root / 'home/alex/src/repo'
    repo.mkdir(parents=True)
    (repo / 'data').write_text('human work')
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    policy = {'machine_id': 'machine1', 'qualified': True, 'policy_qualified': True, 'accounts': {'alex-agent': {
        'uid': 12345, 'owner_uid': os.getuid(), 'homes': ['/home/alex', '/home/alex-agent'],
        'protected': ['/srv/acc'], 'roots': ['/home/alex-agent/workspaces']}}}
    with FolderWorker(state, policy, host_root=root, owner=os.getuid(), lifecycle=FixtureHost()) as worker:
        yield worker, repo


def request(action, **extra):
    return {'action': action, 'operation_id': 'a' * 32, 'machine_id': 'machine1',
            'account': 'alex-agent', 'revision': 0, **extra}


def test_preview_is_metadata_only_and_apply_is_cas_and_idempotent(worker):
    service, repo = worker
    before = (repo / 'data').stat()
    preview = service.handle(request('preview', path='/home/alex/src/repo', method='named_acl', write=False))
    assert preview['count'] == 2
    assert (repo / 'data').stat() == before
    reply = service.handle(request('apply', digest=preview['digest']))
    assert reply['state'] == 'applied'
    assert reply['revision'] == 1
    assert service.handle(request('apply', digest=preview['digest'])) == reply
    assert (repo / 'data').read_text() == 'human work'
    undone = service.handle(request('undo', digest=preview['digest'], revision=1))
    assert undone['state'] == 'undone'
    assert (repo / 'data').stat().st_mode == before.st_mode


def test_replaced_preview_never_mutates(worker):
    from acc.folder_policy import FolderError
    service, repo = worker
    preview = service.handle(request('preview', path='/home/alex/src/repo', method='named_acl', write=True))
    (repo / 'data').rename(repo / 'old')
    (repo / 'data').write_text('replacement')
    with pytest.raises(FolderError, match='changed'):
        service.handle(request('apply', digest=preview['digest']))
    assert (repo / 'data').stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize('extra', [{'uid': 0}, {'machine_id': 'other'}, {'account': 'root'}, {'path': '/etc'}, {'command': 'id'}])
def test_worker_refuses_untrusted_coordinates(worker, extra):
    from acc.folder_policy import FolderError
    service, _ = worker
    with pytest.raises(FolderError):
        service.handle(request('preview', **({'path': '/home/alex/src/repo', 'method': 'named_acl', 'write': True} | extra)))


def test_nested_links_and_hardlinks_fail_before_any_mutation(worker):
    from acc.folder_policy import FolderError
    service, repo = worker
    (repo / 'alias').symlink_to('data')
    with pytest.raises(FolderError):
        service.handle(request('preview', path='/home/alex/src/repo', method='named_acl', write=True))
    (repo / 'alias').unlink()
    os.link(repo / 'data', repo / 'hard')
    with pytest.raises(FolderError):
        service.handle(request('preview', path='/home/alex/src/repo', method='named_acl', write=True))


def test_policy_preview_is_separate_from_os_grant_and_requires_reload_ack(worker):
    service, repo = worker
    acknowledged = []
    service.reload = lambda account, payload: acknowledged.append(payload) or {'revision': payload['revision']}
    before = (repo / 'data').stat().st_mode
    message = request('policy_preview', roots=['/home/alex-agent/workspaces', '/home/alex/src/repo'], allowed_folders=True)
    preview = service.handle(message)
    assert (repo / 'data').stat().st_mode == before
    reply = service.handle(request('policy_apply', digest=preview['digest']))
    assert reply['state'] == 'applied'
    assert service.read('account-alex-agent.json')['roots'] == message['roots']
    assert (repo / 'data').stat().st_mode == before
    assert acknowledged[-1]['allowed_folders'] is True


def test_policy_off_never_allows_protected_roots(worker):
    from acc.folder_policy import FolderError
    service, _ = worker
    with pytest.raises(FolderError):
        service.handle(request('policy_preview', roots=['/home/alex-agent/workspaces', '/etc'], allowed_folders=False))


def test_policy_reload_failure_stays_resumable_without_a_second_revision(worker):
    from acc.folder_policy import FolderError
    service, _ = worker
    preview = service.handle(request('policy_preview', roots=['/home/alex-agent/workspaces', '/home/alex/src/repo'], allowed_folders=True))
    service.reload = lambda *_: None
    with pytest.raises(FolderError):
        service.handle(request('policy_apply', digest=preview['digest']))
    assert service.read('account-alex-agent.json') is None
    assert service.read('a' * 32 + '.json')['state'] == 'pending'
    service.reload = lambda _, value: {'revision': value['revision']}
    result = service.handle(request('policy_apply', digest=preview['digest']))
    assert result == {'state': 'applied', 'revision': 1}
    # A broker restart loses its in-memory policy. A reviewed retry must reload,
    # not return an old cached acknowledgement without checking the target.
    service.reload = lambda *_: None
    with pytest.raises(FolderError):
        service.handle(request('policy_apply', digest=preview['digest']))


def test_undo_reports_new_descendants_instead_of_claiming_complete_revocation(worker):
    service, repo = worker
    preview = service.handle(request('preview', path='/home/alex/src/repo', method='named_acl', write=True))
    service.handle(request('apply', digest=preview['digest']))
    (repo / 'later').write_text('human work after grant')
    result = service.handle(request('undo', digest=preview['digest'], revision=1))
    assert result['state'] == 'repair'
    assert result['conflicts'] >= 1
    assert (repo / 'later').read_text() == 'human work after grant'


def test_overlapping_grant_cannot_be_undone_over_another_grant(worker):
    from acc.folder_policy import FolderError
    service, _ = worker
    preview = service.handle(request('preview', path='/home/alex/src/repo', method='named_acl', write=True))
    service.handle(request('apply', digest=preview['digest']))
    with pytest.raises(FolderError, match='overlap'):
        service.handle(request('preview', path='/home/alex/src/repo', method='named_acl', write=False,
            operation_id='b' * 32, revision=1))


def test_ancestor_traverse_grants_are_reference_counted_and_never_listable(worker):
    import base64

    from acc.folder_acl import ACL, snapshot
    service, repo = worker
    home = repo.parents[1]
    home.chmod(0o700)
    repo.parent.chmod(0o700)
    second = repo.with_name('second')
    second.mkdir(mode=0o700)
    def rights():
        fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY)
        try:
            raw = snapshot(fd)['access']
            return ACL.decode(base64.b64decode(raw)).effective(2, 12345) if raw else 0
        finally:
            os.close(fd)
    first = service.handle(request('preview', path='/home/alex/src/repo', method='named_acl', write=True))
    service.handle(request('apply', digest=first['digest']))
    assert rights() == 1
    other = service.handle(request('preview', path='/home/alex/src/second', method='named_acl', write=True,
        operation_id='b' * 32, revision=1))
    service.handle(request('apply', digest=other['digest'], operation_id='b' * 32, revision=1))
    service.handle(request('undo', digest=first['digest'], revision=2))
    assert rights() == 1
    service.handle(request('undo', digest=other['digest'], operation_id='b' * 32, revision=3))
    assert rights() == 0


def test_ancestor_traversal_refuses_exposing_unrelated_private_files(worker):
    from acc.folder_policy import FolderError
    service, repo = worker
    home = repo.parents[1]
    home.chmod(0o700)
    (home / 'unrelated.txt').write_text('not in this project')
    with pytest.raises(FolderError, match='unrelated'):
        service.handle(request('preview', path='/home/alex/src/repo', method='named_acl', write=True))


@pytest.mark.parametrize('qualified', [False, None, 'false', 1])
def test_only_literal_qualification_true_can_enable_os_mutations(worker, qualified):
    from acc.folder_policy import FolderError
    service, repo = worker
    service.policy['qualified'] = qualified
    before = (repo / 'data').stat().st_mode
    preview = service.handle(request('preview', path='/home/alex/src/repo', method='named_acl', write=True))
    with pytest.raises(FolderError, match='qualified'):
        service.handle(request('apply', digest=preview['digest']))
    assert (repo / 'data').stat().st_mode == before


def test_preview_budget_includes_ancestor_sibling_inspection(worker, monkeypatch):
    from acc import folder_worker
    from acc.folder_policy import FolderError
    service, repo = worker
    home = repo.parents[1]
    home.chmod(0o700)
    repo.parent.chmod(0o700)
    for number in range(10):
        path = home / f'private-{number}'
        path.write_text('not part of this project')
        path.chmod(0o600)
    monkeypatch.setattr(folder_worker, 'MAX_ENTRIES', 5)
    with pytest.raises(FolderError, match='budget'):
        service.handle(request('preview', path='/home/alex/src/repo', method='named_acl', write=True))


def test_ack_journal_failure_after_policy_publication_is_recoverable(worker, monkeypatch):
    service, _ = worker
    service.reload = lambda _, payload: {'revision': payload['revision']}
    preview = service.handle(request('policy_preview', roots=['/home/alex-agent/workspaces', '/home/alex/src/repo'], allowed_folders=True))
    write = service.write
    def fail_result(name, value):
        if name == 'a' * 32 + '.json' and value.get('result'):
            raise OSError('synthetic full disk before final acknowledgement')
        write(name, value)
    monkeypatch.setattr(service, 'write', fail_result)
    with pytest.raises(OSError):
        service.handle(request('policy_apply', digest=preview['digest']))
    assert service.read('account-alex-agent.json')['revision'] == 1
    monkeypatch.setattr(service, 'write', write)
    assert service.handle(request('policy_apply', digest=preview['digest'])) == {'state': 'applied', 'revision': 1}


@pytest.mark.parametrize('offset', [0, 1])
def test_expired_preview_refuses_at_the_deadline_without_acl_changes(worker, monkeypatch, offset):
    from acc import folder_worker
    from acc.folder_policy import FolderError
    service, repo = worker
    before = (repo / 'data').stat().st_mode
    preview = service.handle(request('preview', path='/home/alex/src/repo', method='named_acl', write=True))
    monkeypatch.setattr(folder_worker.time, 'time', lambda: preview['expires_at'] + offset)
    with pytest.raises(FolderError, match='expired'):
        service.handle(request('apply', digest=preview['digest']))
    assert (repo / 'data').stat().st_mode == before


@pytest.mark.parametrize('filesystem', ['9p', 'drvfs', 'fuse.drvfs'])
def test_windows_drive_filesystems_are_existing_access_only_even_when_qualified(worker, monkeypatch, filesystem):
    from acc import folder_worker
    from acc.folder_policy import FolderError
    service, repo = worker
    before = (repo / 'data').stat().st_mode
    monkeypatch.setattr(folder_worker, 'mount_type', lambda _: filesystem, raising=False)
    with pytest.raises(FolderError, match='existing access'):
        service.handle(request('preview', path='/home/alex/src/repo', method='named_acl', write=True))
    assert (repo / 'data').stat().st_mode == before


def test_boolean_broker_revision_is_not_an_acknowledgement(worker):
    from acc.folder_policy import FolderError
    service, _ = worker
    service.reload = lambda *_: {'revision': True}
    preview = service.handle(request('policy_preview', roots=['/home/alex-agent/workspaces', '/home/alex/src/repo'], allowed_folders=True))
    with pytest.raises(FolderError):
        service.handle(request('policy_apply', digest=preview['digest']))
    assert service.read('account-alex-agent.json') is None


@pytest.mark.parametrize('qualification', [False, None, 'true', 1])
def test_unqualified_policy_worker_cannot_publish_or_prepare_policy_changes(worker, qualification):
    from acc.folder_policy import FolderError
    service, _ = worker
    service.policy['policy_qualified'] = qualification
    with pytest.raises(FolderError, match='qualified'):
        service.handle(request('policy_preview', roots=['/home/alex-agent/workspaces', '/home/alex/src/repo'], allowed_folders=True))
    assert service.read('account-alex-agent.json') is None


@pytest.mark.parametrize('allowed', [False, True])
def test_policy_roots_refuse_mount_boundaries(worker, monkeypatch, allowed):
    from acc import folder_policy
    service, repo = worker
    original = folder_policy.mount_id
    def mount(fd):
        return 987654 if os.readlink(f'/proc/self/fd/{fd}') == str(repo) else original(fd)
    monkeypatch.setattr(folder_policy, 'mount_id', mount)
    with pytest.raises(folder_policy.FolderError, match='[Mm]ount'):
        service.handle(request('policy_preview', roots=['/home/alex-agent/workspaces', '/home/alex/src/repo'], allowed_folders=allowed))


def test_policy_apply_rechecks_mount_ancestry_after_preview(worker, monkeypatch):
    from acc import folder_policy
    service, repo = worker
    service.reload = lambda *args: pytest.fail('An unqualified mount must not be published')
    preview = service.handle(request('policy_preview', roots=['/home/alex-agent/workspaces', '/home/alex/src/repo'], allowed_folders=False))
    original = folder_policy.mount_id
    # The final inode stays identical; only an intermediate ancestor is rebound.
    def mount(fd):
        return 987654 if os.readlink(f'/proc/self/fd/{fd}') == str(repo.parent) else original(fd)
    monkeypatch.setattr(folder_policy, 'mount_id', mount)
    with pytest.raises(folder_policy.FolderError, match='[Mm]ount'):
        service.handle(request('policy_apply', digest=preview['digest']))


@pytest.mark.parametrize('change', ['deleted', 'mapping'])
@pytest.mark.parametrize('lost_ack', [False, True])
def test_w1_started_policy_can_recover_above_abandoned_revision(worker, change, lost_ack):
    import shutil

    from acc.folder_policy import FolderError
    service, repo = worker
    broker = {}
    fail = True
    def reload(_, payload):
        nonlocal fail
        if broker and payload['revision'] <= broker['revision'] and payload != broker:
            raise FolderError('Broker refuses conflicting equal/lower revision')
        if not fail or lost_ack:
            broker.clear()
            broker.update(payload)
        if fail:
            fail = False
            raise OSError('lost reload acknowledgement')
        return {'revision': payload['revision']}
    service.reload = reload
    reviewed = service.handle(request('policy_preview', roots=['/home/alex-agent/workspaces', '/home/alex/src/repo'], allowed_folders=False))
    with pytest.raises(OSError):
        service.handle(request('policy_apply', digest=reviewed['digest']))
    if change == 'deleted':
        shutil.rmtree(repo)
    else:
        service.policy['accounts']['alex-agent']['homes'].append('/home/blair')
    status = service.handle(request('status'))
    assert status['operation']['can_complete'] is False
    recovered = service.handle(request('policy_recover', revision=1, digest=reviewed['digest'], recovery_digest=status['recovery_digest']))
    assert recovered == {'state': 'recovered', 'revision': 2, 'roots': ['/home/alex-agent/workspaces'], 'allowed_folders': True}
    assert broker['revision'] == 2 and broker['allowed_folders'] is True
    assert broker['roots'] == ['/home/alex-agent/workspaces']
    with pytest.raises(FolderError):
        service.handle(request('policy_apply', digest=reviewed['digest']))
    assert service.handle(request('policy_recover', revision=1, digest=reviewed['digest'], recovery_digest=status['recovery_digest'])) == recovered


def test_w1_status_expires_preview_before_mapping_refusal(worker, monkeypatch):
    from acc import folder_worker
    from acc.folder_policy import FolderError
    service, _ = worker
    reviewed = service.handle(request('policy_preview', roots=['/home/alex-agent/workspaces'], allowed_folders=False))
    service.policy['accounts']['alex-agent']['protected'].append('/private')
    monkeypatch.setattr(folder_worker.time, 'time', lambda: reviewed['expires_at'] + 1)
    status = service.handle(request('status'))
    assert status['operation']['state'] == 'expired'
    assert status['operation']['can_complete'] is False
    assert status['operation']['mapping_changed'] is True
    with pytest.raises(FolderError):
        service.handle(request('policy_apply', digest=reviewed['digest']))


def test_w1_recovery_reply_loss_and_new_mapping_require_fresh_consent_and_higher_revision(worker):
    from acc.folder_policy import FolderError
    service, _ = worker
    service.reload = lambda *_: (_ for _ in ()).throw(OSError('broker restarting'))
    reviewed = service.handle(request('policy_preview', roots=['/home/alex-agent/workspaces'], allowed_folders=False))
    with pytest.raises(OSError):
        service.handle(request('policy_apply', digest=reviewed['digest']))
    status = service.handle(request('status'))
    broker = {}
    def lose_ack(_, payload):
        broker.update(payload)
        raise OSError('recovery reply lost')
    service.reload = lose_ack
    recovery = request('policy_recover', revision=1, digest=reviewed['digest'], recovery_digest=status['recovery_digest'])
    with pytest.raises(OSError):
        service.handle(recovery)
    assert broker['revision'] == 2
    service.policy['accounts']['alex-agent']['protected'].append('/private')
    with pytest.raises(FolderError, match='status changed'):
        service.handle(recovery)
    status = service.handle(request('status'))
    def reload(_, payload):
        assert payload['revision'] > broker['revision']
        broker.update(payload)
        return {'revision': payload['revision']}
    service.reload = reload
    result = service.handle({**recovery, 'recovery_digest': status['recovery_digest']})
    assert result['revision'] == 3 and result['allowed_folders'] is True
    assert result['roots'] == ['/home/alex-agent/workspaces']


def test_w1_fresh_recovery_review_advances_past_a_rejected_rollback(worker):
    from acc.folder_policy import FolderError
    service, _ = worker
    service.reload = lambda *_: (_ for _ in ()).throw(OSError('lost first reply'))
    reviewed = service.handle(request('policy_preview', roots=['/home/alex-agent/workspaces'], allowed_folders=False))
    with pytest.raises(OSError):
        service.handle(request('policy_apply', digest=reviewed['digest']))
    status = service.handle(request('status'))
    def ahead(_, payload):
        if payload['revision'] <= 2:
            raise FolderError('broker ahead of restored worker state')
        return {'revision': payload['revision']}
    service.reload = ahead
    recovery = request('policy_recover', revision=1, digest=reviewed['digest'], recovery_digest=status['recovery_digest'])
    with pytest.raises(FolderError):
        service.handle(recovery)
    fresh = service.handle(request('status'))
    assert fresh['recovery_digest'] != status['recovery_digest']
    result = service.handle({**recovery, 'recovery_digest': fresh['recovery_digest']})
    assert result == {'state': 'recovered', 'revision': 3, 'roots': ['/home/alex-agent/workspaces'], 'allowed_folders': True}
