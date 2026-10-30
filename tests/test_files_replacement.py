"""Replacing browser bytes must never discard an intervening external edit."""
from pathlib import Path

import pytest
from isolated_settings import isolated_broker
from test_files import call
from test_files_upload import upload


@pytest.fixture
def folder(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    return isolated_broker(tmp_path, allowed_roots=(tmp_path,), run_dir=tmp_path / 'run'), root


def save(folder, action, raw, revision):
    if action == 'upload':
        return upload(folder, 'report.txt', raw.encode(), overwrite='replace', revision=revision)
    return call(folder, 'write', 'report.txt', content=raw, revision=revision)


@pytest.mark.parametrize('action', ['upload', 'write'])
def test_replacement_preserves_last_window_edit(folder, monkeypatch, action):
    from acc.broker import files
    _, root = folder
    target = root / 'report.txt'
    first = upload(folder, 'report.txt', b'original')
    # Interleave at the old destructive syscall and the new move-aside syscall.
    replace, rename = files.os.replace, files.rename_exclusive
    def edit():
        target.write_bytes(b'newer agent edit')
    def racing_replace(src, dst, **kwargs):
        edit()
        return replace(src, dst, **kwargs)
    def racing_rename(src_fd, src, dst_fd, dst):
        if src == 'report.txt':
            edit()
        return rename(src_fd, src, dst_fd, dst)
    monkeypatch.setattr(files.os, 'replace', racing_replace)
    monkeypatch.setattr(files, 'rename_exclusive', racing_rename)
    result = save(folder, action, 'browser bytes', first['revision'])
    contents = [p.read_bytes() for p in root.iterdir() if p.is_file()]
    assert b'newer agent edit' in contents
    assert b'browser bytes' in contents
    assert result['conflict']
    assert (root / result['incoming_copy']).read_bytes() == b'browser bytes'
    assert 'kept' in result['notice'].lower()
    assert not list(root.glob('.acc-save-*'))


@pytest.mark.parametrize('action', ['upload', 'write'])
def test_successful_replacements_retain_all_copies_until_deleted(folder, action):
    _, root = folder
    first = upload(folder, 'report.txt', b'one')
    second = save(folder, action, 'two', first['revision'])
    third = save(folder, action, 'three', second['revision'])
    assert (root / second['previous_copy']).read_bytes() == b'one'
    assert (root / third['previous_copy']).read_bytes() == b'two'
    assert second['previous_copy'] != third['previous_copy']
    assert Path(second['previous_copy']).name.startswith('.')
    assert 'previous copy is kept' in third['notice']
    call(folder, 'delete', second['previous_copy'])
    assert not (root / second['previous_copy']).exists()
    assert (root / third['previous_copy']).read_bytes() == b'two'
    assert (root / 'report.txt').read_bytes() == b'three'
    assert not list(root.glob('.acc-save-*'))


@pytest.mark.parametrize('action', ['upload', 'write'])
def test_symlink_swapped_before_preservation_is_not_followed(folder, tmp_path, monkeypatch, action):
    from acc.broker import files
    _, root = folder
    outside = tmp_path / 'outside.txt'
    outside.write_bytes(b'outside untouched')
    first = upload(folder, 'report.txt', b'original')
    rename = files.rename_exclusive
    def swap(src_fd, src, dst_fd, dst):
        if src == 'report.txt':
            (root / src).unlink()
            (root / src).symlink_to(outside)
        return rename(src_fd, src, dst_fd, dst)
    monkeypatch.setattr(files, 'rename_exclusive', swap)
    result = save(folder, action, 'browser bytes', first['revision'])
    assert result['conflict']
    assert outside.read_bytes() == b'outside untouched'
    assert (root / result['incoming_copy']).read_bytes() == b'browser bytes'
    assert 'outside untouched' not in str(result)


@pytest.mark.parametrize('action', ['upload', 'write'])
def test_recreated_destination_is_never_overwritten(folder, monkeypatch, action):
    from acc.broker import files
    _, root = folder
    first = upload(folder, 'report.txt', b'original')
    link = files.os.link
    def create_before_link(src, dst, **kwargs):
        if dst == 'report.txt':
            (root / dst).write_bytes(b'newly created external file')
        return link(src, dst, **kwargs)
    monkeypatch.setattr(files.os, 'link', create_before_link)
    result = save(folder, action, 'browser bytes', first['revision'])
    assert result['conflict']
    assert (root / 'report.txt').read_bytes() == b'newly created external file'
    assert (root / result['previous_copy']).read_bytes() == b'original'
    assert (root / result['incoming_copy']).read_bytes() == b'browser bytes'


@pytest.mark.parametrize('action', ['upload', 'write'])
def test_recovery_name_collision_never_follows_or_overwrites_symlink(folder, tmp_path, monkeypatch, action):
    from acc.broker import files
    _, root = folder
    outside = tmp_path / 'outside.txt'
    outside.write_bytes(b'outside untouched')
    first = upload(folder, 'report.txt', b'original')
    monkeypatch.setattr(files.time, 'time_ns', lambda: 123)
    monkeypatch.setattr(files.secrets, 'token_hex', lambda n: 'fixed')
    collision = root / '.rynhult-replaced-123-fixed'
    collision.symlink_to(outside)
    result = save(folder, action, 'browser bytes', first['revision'])
    assert result['conflict']
    assert collision.is_symlink()
    assert outside.read_bytes() == b'outside untouched'
    assert (root / 'report.txt').read_bytes() == b'original'
    assert (root / result['incoming_copy']).read_bytes() == b'browser bytes'
    assert not list(root.glob('.acc-save-*'))


@pytest.mark.parametrize('action', ['upload', 'write'])
def test_failed_publication_keeps_both_and_cleans_staging(folder, monkeypatch, action):
    import errno

    from acc.broker import files
    _, root = folder
    first = upload(folder, 'report.txt', b'original')
    link = files.os.link
    def fail(src, dst, **kwargs):
        if dst == 'report.txt':
            raise OSError(errno.ENOSPC, 'fixture disk full')
        return link(src, dst, **kwargs)
    monkeypatch.setattr(files.os, 'link', fail)
    result = save(folder, action, 'browser bytes', first['revision'])
    assert result['conflict']
    assert (root / result['previous_copy']).read_bytes() == b'original'
    assert (root / result['incoming_copy']).read_bytes() == b'browser bytes'
    assert not list(root.glob('.acc-save-*'))


@pytest.mark.parametrize('action', ['upload', 'write'])
async def test_api_returns_recovery_paths_and_audits_no_contents(api, env, project, monkeypatch, action):
    from sqlalchemy import select
    from test_files_upload import batch

    from acc.broker import files
    from acc.server.models import AuditLog
    root = env['workspaces'] / 'demo'
    (root / 'report.txt').write_text('original')
    url = f"/api/files/project/{project['id']}"
    opened = (await api.get(url + '/read', params={'path': 'report.txt'})).json()
    rename = files.rename_exclusive
    def edit(src_fd, src, dst_fd, dst):
        if src == 'report.txt':
            (root / src).write_text('intervening bytes not for audit')
        return rename(src_fd, src, dst_fd, dst)
    monkeypatch.setattr(files, 'rename_exclusive', edit)
    if action == 'upload':
        response = await api.post(url + '/upload', json=batch('report.txt', b'incoming private bytes', overwrite='replace', revision=opened['revision']))
        result = response.json()['files'][0]
    else:
        response = await api.post(url, json={'action': 'write', 'path': 'report.txt', 'content': 'incoming private bytes', 'revision': opened['revision']})
        result = response.json()
    assert response.status_code == 200 and result['conflict']
    assert (root / result['incoming_copy']).read_text() == 'incoming private bytes'
    assert (root / result['previous_copy']).read_text() == 'intervening bytes not for audit'
    with api.app.state.db() as session:
        rows = list(session.scalars(select(AuditLog).where(AuditLog.action == f'files.{action}')))
        assert rows
        detail = str(rows[-1].detail)
        assert result['incoming_copy'] in detail
        assert 'incoming private bytes' not in detail
        assert 'intervening bytes not for audit' not in detail


@pytest.mark.parametrize('action', ['upload', 'write'])
def test_open_external_writer_continues_in_kept_copy(folder, monkeypatch, action):
    from acc.broker import files
    _, root = folder
    first = upload(folder, 'report.txt', b'original')
    link = files.os.link
    with (root / 'report.txt').open('r+b') as writer:
        def write_after_comparison(src, dst, **kwargs):
            if dst == 'report.txt':
                writer.seek(0)
                writer.write(b'late fd edit')
                writer.truncate()
                writer.flush()
            return link(src, dst, **kwargs)
        monkeypatch.setattr(files.os, 'link', write_after_comparison)
        result = save(folder, action, 'browser bytes', first['revision'])
    assert not result.get('conflict')
    assert (root / result['previous_copy']).read_bytes() == b'late fd edit'
    assert (root / 'report.txt').read_bytes() == b'browser bytes'
    assert not list(root.glob('.acc-save-*'))


@pytest.mark.parametrize('action', ['upload', 'write'])
def test_moved_aside_name_is_read_without_following_symlinks(folder, tmp_path, monkeypatch, action):
    from acc.broker import files
    _, root = folder
    outside = tmp_path / 'outside.txt'
    outside.write_text('outside untouched')
    first = upload(folder, 'report.txt', b'original')
    rename = files.rename_exclusive
    def swap_after_move(src_fd, src, dst_fd, dst):
        result = rename(src_fd, src, dst_fd, dst)
        if src == 'report.txt':
            (root / dst).unlink()
            (root / dst).symlink_to(outside)
        return result
    monkeypatch.setattr(files, 'rename_exclusive', swap_after_move)
    result = save(folder, action, 'browser bytes', first['revision'])
    assert result['conflict']
    assert outside.read_text() == 'outside untouched'
    assert 'outside untouched' not in str(result)
    assert (root / result['incoming_copy']).read_bytes() == b'browser bytes'


@pytest.mark.parametrize('action', ['upload', 'write'])
def test_nested_recovery_stays_in_original_directory(folder, action):
    _, root = folder
    (root / 'sub').mkdir()
    first = upload(folder, 'sub/report.txt', b'original')
    if action == 'upload':
        result = upload(folder, 'sub/report.txt', b'new bytes', overwrite='replace', revision=first['revision'])
    else:
        result = call(folder, 'write', 'sub/report.txt', content='new bytes', revision=first['revision'])
    kept = root / result['previous_copy']
    assert kept.parent == root / 'sub'
    assert kept.read_bytes() == b'original'
    assert (root / 'sub/report.txt').read_bytes() == b'new bytes'
    assert sorted(p.name for p in root.iterdir()) == ['sub']


@pytest.mark.parametrize('action', ['upload', 'write'])
def test_disappeared_original_reports_only_existing_recovery_copy(folder, monkeypatch, action):
    from acc.broker import files
    _, root = folder
    first = upload(folder, 'report.txt', b'original')
    rename = files.rename_exclusive
    def remove(src_fd, src, dst_fd, dst):
        if src == 'report.txt':
            (root / src).unlink()
        return rename(src_fd, src, dst_fd, dst)
    monkeypatch.setattr(files, 'rename_exclusive', remove)
    result = save(folder, action, 'browser bytes', first['revision'])
    assert result['conflict']
    assert not result.get('previous_copy')
    assert (root / result['incoming_copy']).read_bytes() == b'browser bytes'
    assert 'Both copies' not in result['notice']
