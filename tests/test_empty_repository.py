"""First commit recovery uses the owning broker and never pushes."""
import subprocess
from pathlib import Path

import pytest
from test_git_identity import person, plain_git


@pytest.mark.parametrize('kind', ['folder', 'git', 'clone'])
async def test_empty_project_first_commit_and_worktree(api, env, kind):
    await person(api, env)
    root = env['workspaces'] / ('empty-' + kind)
    if kind == 'clone':
        remote = env['workspaces'] / 'remote.git'
        subprocess.run(['git', 'init', '--bare', str(remote)], check=True, capture_output=True)
        subprocess.run(['git', 'clone', str(remote), str(root)], check=True, capture_output=True)
    else:
        root.mkdir()
        if kind == 'git':
            plain_git(root, 'init', '-b', 'main')
    created = await api.post('/api/projects', json={'name': 'Empty example', 'slug': 'empty-' + kind,
                                                  'workspace_path': str(root), 'trusted': True})
    assert created.status_code == 201, created.text
    pid = created.json()['id']
    assert (await api.post(f'/api/projects/{pid}/trust')).status_code == 200
    status = await api.get(f'/api/projects/{pid}/first-commit')
    assert status.status_code == 200, status.text
    assert status.json()['needed'] is True
    launch = await api.post('/api/sessions', json={'project_id': pid, 'provider': 'claude', 'name': 'Empty launch', 'worktree': True})
    assert launch.status_code == 409, launch.text
    assert 'Make the first commit' in launch.json()['detail']
    result = await api.post(f'/api/projects/{pid}/first-commit')
    assert result.status_code == 200, result.text
    assert (root / 'README.md').read_text().startswith('# Empty example\n')
    assert plain_git(root, 'log', '-1', '--format=%an <%ae>') == 'casey <casey@noreply.rynhult.invalid>'
    head = plain_git(root, 'rev-parse', 'HEAD')
    # Double submit is a no-op, and nothing was pushed to the empty remote.
    assert (await api.post(f'/api/projects/{pid}/first-commit')).status_code == 200
    assert plain_git(root, 'rev-parse', 'HEAD') == head
    if kind == 'clone':
        assert subprocess.run(['git', '--git-dir', str(remote), 'show-ref'], capture_output=True).returncode == 1
    session = await api.post('/api/sessions', json={'project_id': pid, 'provider': 'claude', 'name': 'Empty launch', 'worktree': True})
    assert session.status_code == 201, session.text


async def test_existing_commit_is_untouched(api, env):
    await person(api, env)
    project = (await api.post('/api/projects/new', json={'name': 'Existing', 'source': 'empty'})).json()
    root = Path(project['workspace_path'])
    before = plain_git(root, 'rev-parse', 'HEAD'), (root / 'README.md').read_bytes()
    assert (await api.get(f"/api/projects/{project['id']}/first-commit")).json()['needed'] is False
    assert (await api.post(f"/api/projects/{project['id']}/first-commit")).status_code == 200
    assert before == (plain_git(root, 'rev-parse', 'HEAD'), (root / 'README.md').read_bytes())


def test_first_commit_preserves_existing_readme_and_other_staged_files(env):
    from acc.broker import ops
    root = env['workspaces'] / 'preserve'
    root.mkdir()
    plain_git(root, 'init', '-b', 'main')
    (root / 'README.md').write_text('Owner content\n')
    with pytest.raises(ops.OpError, match='will not overwrite'):
        ops.project_first_commit(env['broker'], str(root), 'Project', True)
    assert (root / 'README.md').read_text() == 'Owner content\n'
    (root / 'README.md').unlink()
    (root / 'private.txt').write_text('Do not commit this\n')
    plain_git(root, 'add', 'private.txt')
    hooks = root / '.git/hooks/pre-commit'
    hooks.write_text('#!/bin/sh\ntouch should-not-run\nexit 1\n')
    hooks.chmod(0o755)
    ops.project_first_commit(env['broker'], str(root), 'Project', True, 'Author', 'author@example.com')
    assert plain_git(root, 'ls-tree', '--name-only', 'HEAD') == 'README.md'
    assert plain_git(root, 'diff', '--cached', '--name-only') == 'private.txt'
    assert not (root / 'should-not-run').exists()


async def test_import_empty_github_repository_offers_first_commit(api, env, monkeypatch):
    from acc.broker import ops
    await person(api, env)

    def empty_clone(config, repo, dest):
        Path(dest).mkdir()
        plain_git(dest, 'init', '-b', 'main')
        return {'path': dest}

    monkeypatch.setattr(ops, 'clone_repo', empty_clone)
    response = await api.post('/api/projects/import', json={'repo': 'example/empty-repo', 'trust': False})
    assert response.status_code == 201, response.text
    project = response.json()
    assert (await api.get(f"/api/projects/{project['id']}/first-commit")).json()['needed']
    assert not (Path(project['workspace_path']) / 'README.md').exists()
    assert (await api.post(f"/api/projects/{project['id']}/first-commit")).status_code == 200


async def test_team_on_empty_repository_gets_actionable_conflict(api, env):
    from test_team import grant
    await person(api, env)
    root = env['workspaces'] / 'empty-team'
    root.mkdir()
    plain_git(root, 'init', '-b', 'main')
    project = await api.post('/api/projects', json={'name': 'Empty team', 'slug': 'empty-team',
                                                  'workspace_path': str(root)})
    pid = project.json()['id']
    await api.post(f'/api/projects/{pid}/trust')
    response = await api.post('/api/sessions', json={'project_id': pid, 'provider': 'claude', 'name': 'Lead',
                                                   'worktree': True, 'orchestrator': grant()})
    assert response.status_code == 409, response.text
    assert 'Make the first commit' in response.json()['detail']
    (root / 'README.md').write_text('Existing draft\n')
    refused = await api.post(f'/api/projects/{pid}/first-commit')
    assert refused.status_code == 409, refused.text
    assert 'will not overwrite' in refused.json()['detail']


@pytest.mark.parametrize('boundary', ['probe', 'identity', 'metadata'])
def test_first_commit_never_follows_replaced_workspace(env, tmp_path, monkeypatch, boundary):
    from acc.broker import ops
    root = env['workspaces'] / 'swap-target'
    root.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    if boundary != 'probe':
        for path in (root, outside):
            plain_git(path, 'init', '-b', 'main')
        (outside / 'README.md').write_text('Committed outside content\n')
        plain_git(outside, 'add', 'README.md')
        plain_git(outside, '-c', 'user.name=Outside', '-c', 'user.email=outside@example.com',
                  'commit', '--allow-empty', '-qm', 'Existing outside commit')
        (outside / 'README.md').write_text('Uncommitted outside content\n')
    original = ops.subprocess.run
    swapped = False

    def interleave(args, **kwargs):
        nonlocal swapped
        if boundary != 'probe' and not swapped and ('commit' in args or 'commit-tree' in args):
            swapped = True
            if boundary == 'metadata':
                (root / '.git').rename(root / 'saved-git')
                (root / '.git').symlink_to(outside / '.git', target_is_directory=True)
            else:
                root.rename(root.with_name('saved-target'))
                root.symlink_to(outside, target_is_directory=True)
        result = original(args, **kwargs)
        if boundary == 'probe' and not swapped and '--show-toplevel' in args:
            swapped = True
            root.rename(root.with_name('saved-target'))
            root.symlink_to(outside, target_is_directory=True)
        return result

    monkeypatch.setattr(ops.subprocess, 'run', interleave)
    before = plain_git(outside, 'rev-parse', 'HEAD') if boundary != 'probe' else None
    try:
        ops.project_first_commit(env['broker'], str(root), 'Safe project', True, 'Author', 'author@example.com')
    except ops.OpError:
        pass  # Failing closed or finishing against the pinned original directory are both safe.
    assert swapped, 'the proof must reach its controlled interleaving'
    if boundary == 'probe':
        assert list(outside.iterdir()) == []
    else:
        assert plain_git(outside, 'rev-parse', 'HEAD') == before
        assert (outside / 'README.md').read_text() == 'Uncommitted outside content\n'


def test_orphan_head_is_not_an_empty_repository(env):
    from acc.broker import ops
    root = env['workspaces'] / 'orphan'
    root.mkdir()
    plain_git(root, 'init', '-b', 'main')
    plain_git(root, '-c', 'user.name=Author', '-c', 'user.email=author@example.com',
              'commit', '--allow-empty', '-qm', 'Existing history')
    plain_git(root, 'checkout', '--orphan', 'new-branch')
    for create in (False, True):
        with pytest.raises(ops.OpError, match='history'):
            ops.project_first_commit(env['broker'], str(root), 'Orphan', create)
    assert plain_git(root, 'rev-list', '--all', '--count') == '1'
    assert not (root / 'README.md').exists()


def test_first_commit_refuses_history_created_during_identity_read(env, monkeypatch):
    from acc.broker import ops
    root = env['workspaces'] / 'concurrent-head'
    root.mkdir()
    plain_git(root, 'init', '-b', 'main')
    original = ops.subprocess.run
    advanced = False

    def concurrent_commit(args, **kwargs):
        nonlocal advanced
        result = original(args, **kwargs)
        if not advanced and args[-2:] == ['--get', 'user.email']:
            advanced = True
            plain_git(root, '-c', 'user.name=Other writer', '-c', 'user.email=other@example.com',
                      'commit', '--allow-empty', '-qm', 'Concurrent first commit')
        return result

    monkeypatch.setattr(ops.subprocess, 'run', concurrent_commit)
    with pytest.raises(ops.OpError, match='changed'):
        ops.project_first_commit(env['broker'], str(root), 'Project', True, 'Author', 'author@example.com')
    assert advanced
    assert plain_git(root, 'rev-list', '--all', '--count') == '1'
    assert plain_git(root, 'log', '-1', '--format=%s') == 'Concurrent first commit'
    assert not (root / 'README.md').exists()


@pytest.mark.parametrize('entry', ['.git', 'objects', 'refs', 'info', 'pack', 'heads', 'tags'])
def test_first_commit_bootstrap_never_initializes_through_links(env, tmp_path, monkeypatch, entry):
    from acc.broker import ops
    root = env['workspaces'] / ('bootstrap-' + entry.strip('.'))
    root.mkdir()
    outside = tmp_path / 'outside-bootstrap'
    outside.mkdir()
    original_run, original_mkdir = ops.subprocess.run, ops.os.mkdir
    injected = False

    def inject_before_git_init(args, **kwargs):
        nonlocal injected
        if not injected and 'init' in args:
            injected = True
            if entry == '.git':
                (root / '.git').symlink_to(outside, target_is_directory=True)
            else:
                original_mkdir(root / '.git')
                parent = root / '.git'
                if entry in ('info', 'pack', 'heads', 'tags'):
                    parent = parent / ('objects' if entry in ('info', 'pack') else 'refs')
                    original_mkdir(parent)
                (parent / entry).symlink_to(outside, target_is_directory=True)
        return original_run(args, **kwargs)

    def inject_before_reservation(path, mode=0o777, *, dir_fd=None):
        nonlocal injected
        # Also cover a descriptor-relative bootstrap that no longer calls git init.
        if not injected and str(path) == entry and dir_fd is not None:
            injected = True
            ops.os.symlink(str(outside), entry, dir_fd=dir_fd, target_is_directory=True)
        return original_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(ops.subprocess, 'run', inject_before_git_init)
    monkeypatch.setattr(ops.os, 'mkdir', inject_before_reservation)
    try:
        ops.project_first_commit(env['broker'], str(root), 'Bootstrap', True, 'Author', 'author@example.com')
    except ops.OpError:
        pass
    assert injected, 'the proof must reach the bootstrap mutation boundary'
    assert list(outside.iterdir()) == []
    assert not (root / 'README.md').exists()


@pytest.mark.parametrize('entry', ['index', 'object-fanout'])
def test_first_commit_metadata_links_cannot_redirect_git_writes(env, tmp_path, entry):
    import hashlib

    from acc.broker import ops
    root = env['workspaces'] / ('metadata-' + entry)
    root.mkdir()
    plain_git(root, 'init', '-b', 'main')
    outside = tmp_path / 'outside-metadata'
    outside.mkdir()
    if entry == 'index':
        other = tmp_path / 'other-repository'
        other.mkdir()
        plain_git(other, 'init', '-b', 'main')
        (other / 'private.txt').write_text('Other staged content\n')
        plain_git(other, 'add', 'private.txt')
        external = outside / 'index'
        external.write_bytes((other / '.git/index').read_bytes())
        (root / '.git/index').symlink_to(external)
    else:
        content = b'# Project\n\nStarted from Rynhult. Session notes: docs/handoffs/.\n'
        digest = hashlib.sha1(b'blob ' + str(len(content)).encode() + b'\0' + content).hexdigest()
        (root / '.git/objects' / digest[:2]).symlink_to(outside, target_is_directory=True)
    before = {p.name: p.read_bytes() for p in outside.iterdir()}
    try:
        ops.project_first_commit(env['broker'], str(root), 'Project', True, 'Author', 'author@example.com')
    except ops.OpError:
        pass
    assert before == {p.name: p.read_bytes() for p in outside.iterdir()}
