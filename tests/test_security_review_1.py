"""Regression tests for the three findings from security review #1."""
import gzip
import hashlib
import io
import subprocess
import tarfile

import pytest
from isolated_settings import isolated_broker

from acc import bundle
from acc.broker import files, ops


def test_files_root_ancestor_swap_cannot_read_outside_data(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    project = allowed / "project"
    root = project / "workspace"
    root.mkdir(parents=True)
    outside = tmp_path / "outside"
    credentials = outside / "workspace"
    credentials.mkdir(parents=True)
    (credentials / "auth.json").write_text('{"token":"synthetic-private-data"}')
    config = isolated_broker(tmp_path, allowed_roots=(allowed,), run_dir=tmp_path / "run")
    real_open = files._open_dir

    swapped = False

    def swapped_open(workspace, parts):
        nonlocal swapped
        if swapped:
            return real_open(workspace, parts)
        swapped = True
        project.rename(allowed / "original")
        project.symlink_to(outside, target_is_directory=True)
        return real_open(workspace, parts)

    monkeypatch.setattr(files, "_open_dir", swapped_open)
    try:
        result = ops.files(config, str(root), action="read", rel="auth.json")
    except ops.OpError:
        return
    assert "synthetic-private-data" not in result.get("content", "")


def test_bundle_rejects_oversized_extended_metadata(tmp_path):
    path = tmp_path / "release.tar.gz"
    # Highly compressible metadata is consumed by tarfile before member/size validation.
    with tarfile.open(path, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        member = tarfile.TarInfo("acc-0.13.0/file")
        member.size = 1
        member.pax_headers = {"comment": "x" * (8 << 20)}
        archive.addfile(member, io.BytesIO(b"x"))
    assert path.stat().st_size < 16384
    checksum = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError):
        bundle.verify(path, "0.13.0", checksum)


def test_files_rejects_registered_root_inside_git(tmp_path):
    root = tmp_path / "project/.git/hooks"
    root.mkdir(parents=True)
    (root / "pre-commit").write_text("synthetic-hook")
    config = isolated_broker(tmp_path, allowed_roots=(tmp_path,), run_dir=tmp_path / "run")
    with pytest.raises(ops.OpError):
        ops.files(config, str(root), action="read", rel="pre-commit")


@pytest.mark.parametrize('action', ['list', 'read', 'create', 'mkdir', 'delete', 'rename'])
@pytest.mark.parametrize('suffix', ['.git/hooks', '.env.local/nested', '.ssh', '.codex'])
def test_files_applies_protection_to_registered_root(tmp_path, action, suffix):
    root = tmp_path / 'project' / suffix
    root.mkdir(parents=True)
    leaf = 'auth.json' if suffix == '.codex' else 'note'
    if action not in ('create', 'mkdir'):
        (root / leaf).write_text('private')
    config = isolated_broker(tmp_path, allowed_roots=(tmp_path,), run_dir=tmp_path / 'run')
    # .codex itself is not secret, but its auth.json is; list is allowed there.
    if suffix == '.codex' and action == 'list':
        assert ops.files(config, str(root), action=action)['entries'][0]['protected']
        return
    with pytest.raises(ops.OpError):
        ops.files(config, str(root), action=action, rel='' if action == 'list' else leaf,
                  destination='other', content='changed')


@pytest.mark.parametrize('action', ['read', 'write', 'rename', 'commit'])
def test_files_retains_open_root_after_ancestor_moves(tmp_path, monkeypatch, action):
    root = tmp_path / 'allowed/project/workspace'
    root.mkdir(parents=True)
    (root / 'note').write_text('original')
    outside = tmp_path / 'outside/workspace'
    outside.mkdir(parents=True)
    (outside / 'note').write_text('outside')
    if action == 'commit':
        from test_changes import repo
        repo(root)
        repo(outside)
    config = isolated_broker(tmp_path, allowed_roots=(tmp_path / 'allowed',), run_dir=tmp_path / 'run')
    opened = ops.files(config, str(root), action='read', rel='note')
    real_read = files._read_at
    moved = False

    def move_after_open(fd, leaf, limit):
        nonlocal moved
        if not moved:
            moved = True
            root.parent.rename(root.parent.with_name('original'))
            root.parent.symlink_to(outside.parent, target_is_directory=True)
        return real_read(fd, leaf, limit)

    real_parent = files.parent
    from contextlib import contextmanager

    @contextmanager
    def move_before_destination(*args):
        with real_parent(*args) as pair:
            if action == 'rename' and not moved:
                move_after_open(pair[0], pair[1], 1024)
            yield pair

    monkeypatch.setattr(files, '_read_at', move_after_open)
    monkeypatch.setattr(files, 'parent', move_before_destination)
    result = ops.files(config, str(root), action='write' if action == 'commit' else action,
                       rel='note', destination='renamed', content='saved', revision=opened['revision'],
                       **({'message': 'Save note', 'name': 'Alex Example', 'email': 'alex@example.com'}
                          if action == 'commit' else {}))
    original = tmp_path / 'allowed/original/workspace'
    assert (outside / 'note').read_text() == 'outside'
    assert not (outside / 'renamed').exists()
    if action == 'commit':
        assert result.get('commit'), result
        assert subprocess.check_output(['git', '-C', str(original), 'show', 'HEAD:note']).strip() == b'saved'
    if action == 'read':
        assert result['content'] == 'original'
    else:
        assert (original / ('renamed' if action == 'rename' else 'note')).read_text() == (
            'original' if action == 'rename' else 'saved')


def archive_bytes(tmp_path, records):
    path = tmp_path / 'bundle.tar.gz'
    with tarfile.open(path, 'w:gz', format=tarfile.PAX_FORMAT) as tar:
        for name, content, pax in records:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.pax_headers = pax
            tar.addfile(info, io.BytesIO(content))
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize('operation', ['verify', 'unpack'])
@pytest.mark.parametrize('budget', ['MAX_MEMBERS', 'MAX_SIZE', 'MAX_METADATA'])
def test_bundle_enforces_cumulative_limits(tmp_path, monkeypatch, operation, budget):
    path, digest = archive_bytes(tmp_path, [
        ('acc-0.13.0/one', b'12345678', {'comment': 'x' * 40}),
        ('acc-0.13.0/two', b'12345678', {'comment': 'y' * 40}),
    ])
    monkeypatch.setattr(bundle, budget, {'MAX_MEMBERS': 1, 'MAX_SIZE': 12, 'MAX_METADATA': 80}[budget], raising=False)
    with pytest.raises(ValueError):
        if operation == 'verify':
            bundle.verify(path, '0.13.0', digest)
        else:
            bundle.unpack(path, tmp_path / 'unpacked', '0.13.0', digest)
    assert not (tmp_path / 'unpacked').exists()


@pytest.mark.parametrize('kind', [tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.GNUTYPE_LONGNAME])
@pytest.mark.parametrize('operation', ['verify', 'unpack'])
def test_bundle_rejects_huge_advertised_metadata_before_read(tmp_path, kind, operation):
    # Only a header, no advertised body: rejection must precede the attempted body read.
    info = tarfile.TarInfo('././@PaxHeader')
    info.type, info.size = kind, 1 << 30
    path = tmp_path / 'bomb.tar.gz'
    path.write_bytes(gzip.compress(info.tobuf()))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match='metadata'):
        if operation == 'verify':
            bundle.verify(path, '0.13.0', digest)
        else:
            bundle.unpack(path, tmp_path / 'unpacked', '0.13.0', digest)


def test_bundle_accepts_bounded_pax_and_extracts_contents(tmp_path):
    path, digest = archive_bytes(tmp_path, [
        ('acc-0.13.0/' + 'nested/' * 20 + 'file', b'contents', {'mtime': '123.25'}),
    ])
    bundle.verify(path, '0.13.0', digest)
    tree = bundle.unpack(path, tmp_path / 'out', '0.13.0', digest)
    assert (tree / ('nested/' * 20) / 'file').read_bytes() == b'contents'


@pytest.mark.parametrize('kind', [tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.GNUTYPE_LONGNAME])
def test_bundle_bounds_nested_extension_headers(tmp_path, kind):
    header = tarfile.TarInfo('././@PaxHeader')
    header.type = kind
    body = b'acc-0.13.0/file\0' if kind == tarfile.GNUTYPE_LONGNAME else b''
    header.size = len(body)
    record = header.tobuf() + body + b'\0' * (-len(body) % 512)
    end = tarfile.TarInfo('acc-0.13.0/file')
    path = tmp_path / 'nested.tar.gz'
    path.write_bytes(gzip.compress(record * 20 + end.tobuf() + b'\0' * 1024))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match='nesting'):
        bundle.verify(path, '0.13.0', digest)


@pytest.mark.parametrize('operation', ['verify', 'unpack'])
def test_bundle_counts_pax_overridden_file_size(tmp_path, monkeypatch, operation):
    path, digest = archive_bytes(tmp_path, [('acc-0.13.0/file', b'x', {'size': '1000000'})])
    monkeypatch.setattr(bundle, 'MAX_SIZE', 100, raising=False)
    with pytest.raises(ValueError, match='oversized'):
        if operation == 'verify':
            bundle.verify(path, '0.13.0', digest)
        else:
            bundle.unpack(path, tmp_path / 'out', '0.13.0', digest)
    assert not (tmp_path / 'out').exists()


def test_bundle_rejects_sparse_pax_before_parsing_map(tmp_path):
    path, digest = archive_bytes(tmp_path, [
        ('acc-0.13.0/file', b'x', {'GNU.sparse.map': '0,1000000000'}),
    ])
    with pytest.raises(ValueError, match='sparse'):
        bundle.verify(path, '0.13.0', digest)


@pytest.mark.parametrize('swap', ['allowed', 'project', 'workspace'])
def test_files_refuses_symlink_in_registered_root_path(tmp_path, swap):
    root = tmp_path / 'allowed/project/workspace'
    root.mkdir(parents=True)
    (root / 'note').write_text('not through a link')
    target = {'allowed': root.parent.parent, 'project': root.parent, 'workspace': root}[swap]
    moved = target.with_name(target.name + '-original')
    target.rename(moved)
    target.symlink_to(moved, target_is_directory=True)
    config = isolated_broker(tmp_path, allowed_roots=(tmp_path / 'allowed',), run_dir=tmp_path / 'run')
    with pytest.raises(ops.OpError):
        ops.files(config, str(root), action='read', rel='note')


def test_bundle_budgets_repeated_global_metadata_before_copying(tmp_path, monkeypatch):
    path = tmp_path / 'global.tar.gz'
    with tarfile.open(path, 'w:gz', format=tarfile.PAX_FORMAT, pax_headers={'comment': 'x' * 40}) as tar:
        for name in ('one', 'two', 'three'):
            info = tarfile.TarInfo('acc-0.13.0/' + name)
            info.size = 1
            tar.addfile(info, io.BytesIO(b'x'))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    # The one global header fits; repeating its fields across members exceeds this budget.
    monkeypatch.setattr(bundle, 'MAX_METADATA', 130)
    with pytest.raises(ValueError, match='metadata'):
        bundle.verify(path, '0.13.0', digest)
