"""Target-local folder checks: no OS grants and no live helper in this suite."""
import os
from pathlib import Path

import pytest


@pytest.mark.parametrize('path', ['/', '/etc/demo', '/home/alex', '/home/blair/repo', '/home/alex/.ssh/repo',
    '/home/alex/src/../repo', '/home/alex//repo', '/home/alex/$HOME/repo', '/tmp/repo', '/home/alex/a:b', '/home/alex/a\n'])
def test_grant_validation_refuses_protected_or_ambiguous_paths(path):
    from acc.folder_policy import FolderError, validate_path
    with pytest.raises(FolderError):
        validate_path(path, homes=['/home/alex', '/home/alex-agent'], protected=['/srv/private'])


def test_descriptor_walk_refuses_symlink_and_identity_swap(tmp_path):
    from acc.folder_policy import FolderError, open_directory
    home = tmp_path / 'home' / 'alex'
    home.mkdir(parents=True)
    (home / 'repo').mkdir()
    with open_directory(str(home / 'repo')) as descriptor:
        identity = descriptor.identity
    (home / 'alias').symlink_to('repo')
    with pytest.raises(FolderError):
        with open_directory(str(home / 'alias')):
            pass
    (home / 'repo').rename(home / 'old')
    (home / 'repo').mkdir()
    with pytest.raises(FolderError):
        with open_directory(str(home / 'repo'), expected=identity):
            pass


def test_acl_mask_does_not_awaken_unrelated_grants():
    from acc.folder_acl import ACL, ACLError
    acl = ACL([(1, 7, -1), (2, 7, 1234), (4, 5, -1), (16, 4, -1), (32, 0, -1)])
    with pytest.raises(ACLError, match='masked'):
        acl.grant(2, 5678, 7)
    changed = ACL([(1, 7, -1), (4, 5, -1), (32, 0, -1)]).grant(2, 5678, 6)
    assert changed.effective(4, -1) == 5
    assert changed.effective(2, 5678) == 6
    assert ACL.decode(changed.encode()) == changed


def test_descriptor_acl_apply_and_conflict_aware_undo(tmp_path):
    from acc.folder_acl import ACLError, grant_fd, restore_fd, snapshot
    path = tmp_path / 'file'
    path.write_text('untouched')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = snapshot(fd)
        after = grant_fd(fd, 12345, write=True)
        assert snapshot(fd) == after
        restore_fd(fd, before, after)
        assert snapshot(fd) == before
        grant_fd(fd, 12345, write=True)
        os.fchmod(fd, 0o600)
        with pytest.raises(ACLError, match='changed'):
            restore_fd(fd, before, after)
        assert Path(path).read_text() == 'untouched'
    finally:
        os.close(fd)


def test_grant_adds_traverse_to_directories_even_when_original_mode_has_no_execute(tmp_path):
    import base64

    from acc.folder_acl import ACL, grant_fd
    path = tmp_path / 'unsearchable'
    path.mkdir(mode=0o600)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        changed = grant_fd(fd, 12345, write=False)
        assert ACL.decode(base64.b64decode(changed['access'])).effective(2, 12345) == 5
    finally:
        os.close(fd)


@pytest.mark.parametrize('path', ['/home/alex/a\ud800', '/home/alex/a\u0085b', '/home/alex/a\u202eb'])
def test_invalid_encoding_and_unicode_controls_are_sanitized_refusals(path):
    from acc.folder_policy import FolderError, literal_path
    with pytest.raises(FolderError):
        literal_path(path)


@pytest.mark.skipif(os.geteuid() == 0, reason='Uses ordinary UID access checks; real separate-UID cases are in the disposable-root files.')
def test_descriptor_walk_and_files_can_cross_traverse_only_ancestors(tmp_path):
    from acc.broker.files import _open_dir
    from acc.folder_policy import open_directory
    parent = tmp_path / 'private'
    repo = parent / 'repo'
    repo.mkdir(parents=True)
    parent.chmod(0o111)
    try:
        with open_directory(str(repo)) as directory:
            assert os.fstat(directory.fd).st_ino == repo.stat().st_ino
        with open_directory(str(parent), search_only=True) as directory:
            fd = os.open('repo', os.O_RDONLY | os.O_DIRECTORY, dir_fd=directory.fd)
            os.close(fd)
        fd = _open_dir('/', repo.parts[1:])
        try:
            assert os.fstat(fd).st_ino == repo.stat().st_ino
        finally:
            os.close(fd)
    finally:
        parent.chmod(0o700)
