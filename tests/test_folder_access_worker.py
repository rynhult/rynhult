"""Regression: grants must not advance policy or claim access without target verification."""
import base64
import os

import pytest
from folder_host_fixture import FixtureHost
from test_folder_worker import request
from test_folder_worker import worker as worker_fixture

worker = worker_fixture

from acc.folder_acl import ACL, snapshot
from acc.folder_policy import FolderError


def test_access_revision_is_separate_and_requires_stopped_account(worker):
    service, repo = worker
    service.lifecycle = FixtureHost()
    preview = service.handle(request('preview', path='/home/alex/src/repo', method='named_acl', write=False))
    service.lifecycle.blocked = True
    with pytest.raises((ValueError, FolderError), match='running'):
        service.handle(request('apply', digest=preview['digest']))
    assert (repo / 'data').stat().st_mode & 0o777 == 0o644
    service.lifecycle.blocked = False
    result = service.handle(request('apply', digest=preview['digest']))
    assert result['verification'] == {'read': True, 'write': False, 'checked': 2}
    assert service.read('account-alex-agent.json') is None
    assert service.handle(request('status'))['access_revision'] == 1


def test_failed_verification_leaves_partial_undoable_access(worker):
    service, repo = worker
    service.lifecycle = FixtureHost()
    def refused(*args, **kwargs):
        raise FolderError('Agent access verification failed.')
    service.lifecycle.verify = refused
    preview = service.handle(request('preview', path='/home/alex/src/repo', method='named_acl', write=True))
    with pytest.raises(FolderError, match='verification'):
        service.handle(request('apply', digest=preview['digest']))
    status = service.handle(request('status'))
    assert status['operation']['state'] == 'partial'
    assert status['access_revision'] == 0
    reply = service.handle(request('undo', digest=preview['digest']))
    assert reply['state'] == 'undone'
    assert (repo / 'data').read_text() == 'human work'


def test_shared_group_preview_is_exact_and_mask_safe(worker):
    service, repo = worker
    service.lifecycle = FixtureHost()
    service.policy['shared_group_qualified'] = True
    before = (repo / 'data').stat()
    preview = service.handle(request('preview', path='/home/alex/src/repo', method='shared_group', write=True))
    assert preview['group'] == {'name': 'ryn-repo-' + 'a' * 16, 'gid': 62345}
    assert not service.lifecycle.created
    assert preview['changes'][0]['group_after'] == 62345
    # fchown needs root: emulate only that syscall, retaining real fd ACL mutations.
    original = os.fchown
    def own(fd, uid, gid):
        if gid != 62345:
            original(fd, uid, gid)
    from unittest.mock import patch
    with patch('acc.folder_acl.os.fchown', own):
        reply = service.handle(request('apply', digest=preview['digest']))
        assert reply['state'] == 'applied'
        assert service.lifecycle.members == {'alex', 'alex-agent'}
        fd = os.open(repo, os.O_RDONLY | os.O_DIRECTORY)
        try:
            current = snapshot(fd)
            assert current['mode'] & 0o2000
            assert ACL.decode(base64.b64decode(current['access'])).effective(8, 62345) == 7
        finally:
            os.close(fd)
        service.handle(request('undo', digest=preview['digest'], revision=1))
    assert not service.lifecycle.members
    assert (repo / 'data').stat().st_mode == before.st_mode
    assert (repo / 'data').read_text() == 'human work'


def test_shared_group_preserves_original_groups_effective_rights(tmp_path):
    from acc.folder_acl import proposed
    path = tmp_path / 'existing'
    path.write_text('x')
    path.chmod(0o640)
    fd = os.open(path, os.O_RDONLY)
    try:
        before = snapshot(fd)
        acl = ACL.decode(proposed(before, 62345, write=True, group=True))
        assert acl.effective(8, before['gid']) == 4
        assert acl.effective(8, 62345) == 6
    finally:
        os.close(fd)


def test_shared_group_requires_its_own_literal_qualification(worker):
    service, _ = worker
    service.lifecycle = FixtureHost()
    service.policy['shared_group_qualified'] = 'true'
    with pytest.raises(FolderError, match='qualified'):
        service.handle(request('preview', path='/home/alex/src/repo', method='shared_group', write=True))
    assert not service.lifecycle.created


def test_cancel_unstarted_grant_fences_late_apply_without_changing_permissions(worker):
    service, repo = worker
    before = (repo / 'data').stat().st_mode
    preview = service.handle(request('preview', path='/home/alex/src/repo', method='named_acl', write=True))
    canceled = service.handle(request('cancel', digest=preview['digest']))
    assert canceled['state'] == 'cancelled'
    with pytest.raises(FolderError, match='expired|changed|cancel'):
        service.handle(request('apply', digest=preview['digest']))
    assert (repo / 'data').stat().st_mode == before


@pytest.mark.parametrize('qualified,digest_key', [(False, 'reviewed'), (True, 'wrong')])
def test_unqualified_or_unreviewed_apply_does_not_even_restart_broker(worker, qualified, digest_key):
    service, _ = worker
    preview = service.handle(request('preview', path='/home/alex/src/repo', method='named_acl', write=False))
    service.policy['qualified'] = qualified
    with pytest.raises(FolderError):
        service.handle(request('apply', digest=preview['digest'] if digest_key == 'reviewed' else 'f' * 64))
    assert service.lifecycle.stops == 0


def test_preview_lists_exact_traversal_acl_without_list_or_default_grants(worker):
    service, repo = worker
    repo.parents[1].chmod(0o700)
    repo.parent.chmod(0o700)
    preview = service.handle(request('preview', path='/home/alex/src/repo', method='named_acl', write=True))
    assert len(preview['ancestor_changes']) == 2
    assert preview['ancestor_changes'][0]['path'] == '/home/alex'
    assert (2, 1, 12345) in preview['ancestor_changes'][0]['access_after']
    assert preview['ancestor_changes'][0]['default_changed'] is False


def test_reenrolled_machine_cannot_replay_previous_machine_access_review(worker):
    service, repo = worker
    preview = service.handle(request('preview', path='/home/alex/src/repo', method='named_acl', write=True))
    service.policy['machine_id'] = 'machine2'
    before = (repo / 'data').stat().st_mode
    with pytest.raises(FolderError, match='machine|reviewed'):
        service.handle(request('apply', machine_id='machine2', digest=preview['digest']))
    assert service.lifecycle.stops == 0
    assert (repo / 'data').stat().st_mode == before


@pytest.mark.parametrize('directory', [False, True])
def test_readonly_group_replaces_writable_owning_group(tmp_path, monkeypatch, directory):
    from types import SimpleNamespace

    from acc import folder_acl
    path = tmp_path / 'object'
    if directory:
        path.mkdir()
    else:
        path.write_text('fixture')
    path.chmod(0o770 if directory else 0o660)
    fd = os.open(path, os.O_RDONLY)
    try:
        before = snapshot(fd)
        raw = folder_acl.proposed(before, 62345, write=False, group=True, directory=directory)
        acl = ACL.decode(raw)
        assert acl.effective(8, before['gid']) == (7 if directory else 6)
        after = {**before, 'gid': 62345, 'access': base64.b64encode(raw).decode()}
        monkeypatch.setattr(folder_acl, 'snapshot', lambda fd: after)
        monkeypatch.setattr(folder_acl.os, 'fstat', lambda fd: SimpleNamespace(st_uid=before['uid'], st_gid=62345))
        assert folder_acl.effective_fd(fd, 12345, [62345]) == (5 if directory else 4)
    finally:
        os.close(fd)


@pytest.mark.parametrize('write', [False, True])
def test_group_default_conversion_matches_review_and_preserves_old_rights(tmp_path, write):
    from acc.folder_acl import grant_fd, proposed_default
    directory = tmp_path / 'repo'
    directory.mkdir()
    directory.chmod(0o770)
    os.setxattr(directory, 'system.posix_acl_default', ACL([(1, 7, -1), (4, 7, -1), (32, 0, -1)]).encode())
    fd = os.open(directory, os.O_RDONLY)
    try:
        before = snapshot(fd)
        raw = proposed_default(before, 62345, write=write, group=True)
        planned = ACL.decode(raw)
        assert planned.effective(4, -1) == (7 if write else 5)
        assert planned.effective(8, before['gid']) == 7
        # Only fchown needs a disposable root VM; exercise real ACL syscalls here.
        from unittest.mock import patch
        with patch('acc.folder_acl.os.fchown'):
            after = grant_fd(fd, 62345, write=write, group=True)
        assert base64.b64decode(after['default']) == raw
    finally:
        os.close(fd)


@pytest.mark.parametrize('collision', ['owning', 'named'])
def test_shared_group_refuses_preexisting_numeric_gid_rights(collision):
    from acc.folder_acl import ACLError, proposed
    acl = ACL([(1, 6, -1), (4, 6, -1), (16, 6, -1), (32, 0, -1)] +
        ([(8, 6, 62345)] if collision == 'named' else []))
    before = {'mode': 0o660, 'gid': 62345 if collision == 'owning' else 1234,
        'access': base64.b64encode(acl.encode()).decode()}
    with pytest.raises(ACLError, match='group ID'):
        proposed(before, 62345, write=False, group=True)
