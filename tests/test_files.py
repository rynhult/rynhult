"""Files security and mutations, using real filesystem and git repositories."""
import base64
import os
import socket
import subprocess
from pathlib import Path

import pytest
from helpers import verified_post, write_db
from isolated_settings import isolated_broker
from test_changes import git, repo

from acc.broker import ops


@pytest.fixture
def folder(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    config = isolated_broker(tmp_path, allowed_roots=(tmp_path,), run_dir=tmp_path / 'run')
    return config, root


def call(folder, action='list', rel='', **kwargs):
    config, root = folder
    return ops.files(config, str(root), action=action, rel=rel, **kwargs)


def test_browse_read_status_and_conflicting_atomic_save(folder):
    _, root = folder
    repo(root)
    (root / 'app.py').write_text('changed\n')
    (root / 'new.md').write_text('# Hello\n')
    (root / '.gitignore').write_text('ignored\n')
    (root / 'ignored').write_text('x')
    (root / 'sub').mkdir()
    rows = {r['name']: r for r in call(folder)['entries']}
    assert rows['app.py']['status'] == 'modified'
    assert rows['new.md']['status'] == 'new'
    assert rows['ignored']['status'] == 'ignored'
    assert rows['new.md']['size'] == 8 and rows['sub']['kind'] == 'directory'
    opened = call(folder, 'read', 'new.md')
    assert opened['content'] == '# Hello\n'
    saved = call(folder, 'write', 'new.md', content='# Updated\n', revision=opened['revision'])
    assert saved['revision'] != opened['revision']
    assert (root / 'new.md').read_text() == '# Updated\n'
    conflict = call(folder, 'write', 'new.md', content='stale', revision=opened['revision'])
    assert conflict['conflict'] and conflict['current']['content'] == '# Updated\n'
    assert (root / 'new.md').read_text() == '# Updated\n'


@pytest.mark.parametrize('rel', ['../outside', '/etc/passwd', 'sub/../../outside', '.git/config', 'a\\b', 'a/../b'])
def test_bad_paths_refused(folder, rel):
    with pytest.raises(ops.OpError):
        call(folder, 'read', rel)


def test_symlinks_special_files_and_root_escape_refused(folder, tmp_path):
    _, root = folder
    (root / 'plain').write_text('safe')
    (tmp_path / 'outside').write_text('outside')
    (root / 'internal').symlink_to(root / 'plain')
    (root / 'external').symlink_to(tmp_path / 'outside')
    (root / 'dirlink').symlink_to(tmp_path, target_is_directory=True)
    os.mkfifo(root / 'fifo')
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(root / 'socket'))
    try:
        for path in ['internal', 'external', 'dirlink/outside', 'fifo', 'socket']:
            with pytest.raises(ops.OpError):
                call(folder, 'read', path)
            with pytest.raises(ops.OpError):
                call(folder, 'write', path, content='bad', revision='x')
        with pytest.raises(ops.OpError):
            ops.files(folder[0], '/etc', action='list')
    finally:
        sock.close()


@pytest.mark.parametrize('rel', ['.env', '.env.local', 'session.env', 'key.pem', 'id_ed25519', 'credentials.json',
    '.ssh/key', '.aws/credentials', '.config/gh/hosts.yml', '.docker/config.json', '.codex/auth.json',
    '.claude/.credentials.json', '.copilot/config.json', '.config/antigravity/plugin-secrets.json',
    '.gemini/antigravity-cli/antigravity-oauth-token', '.config/cursor/auth.json', '.kube/config'])
def test_secrets_listed_but_never_read_written_or_downloaded(folder, rel):
    _, root = folder
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('protected')
    for action in ['read', 'download', 'write', 'delete']:
        with pytest.raises(ops.OpError):
            call(folder, action, rel, content='bad', revision='x')
    rows = call(folder, rel=str(target.parent.relative_to(root)).replace('.', '', 1) if target.parent == root else str(target.parent.relative_to(root))) if target.parent == root else None
    if rows:
        assert next(r for r in rows['entries'] if r['name'] == target.name)['protected']


def test_size_binary_images_and_templates(folder):
    _, root = folder
    (root / 'huge').write_bytes(b'x' * (1024 * 1024 + 1))
    assert call(folder, 'read', 'huge')['too_large']
    (root / 'blob').write_bytes(b'\0abc')
    assert call(folder, 'read', 'blob')['binary']
    assert base64.b64decode(call(folder, 'download', 'blob')['data']) == b'\0abc'
    (root / 'photo.png').write_bytes(b'\x89PNG\r\n\x1a\n\0')
    assert call(folder, 'read', 'photo.png')['media_type'] == 'image/png'
    (root / '.env.example').write_text('EXAMPLE=placeholder')
    assert call(folder, 'read', '.env.example')['content'] == 'EXAMPLE=placeholder'
    (root / 'oversize').write_bytes(b'\0' * (4 * 1024 * 1024 + 1))
    with pytest.raises(ops.OpError):
        call(folder, 'download', 'oversize')


def test_create_rename_delete_no_overwrite_or_recursive_delete(folder):
    _, root = folder
    call(folder, 'mkdir', 'notes')
    call(folder, 'create', 'notes/one.md', content='first')
    with pytest.raises(ops.OpError):
        call(folder, 'create', 'notes/one.md', content='overwritten')
    call(folder, 'create', 'notes/two.md', content='second')
    with pytest.raises(ops.OpError):
        call(folder, 'rename', 'notes/one.md', destination='notes/two.md')
    with pytest.raises(ops.OpError):
        call(folder, 'delete', 'notes')
    call(folder, 'rename', 'notes/one.md', destination='notes/renamed.md')
    assert (root / 'notes/renamed.md').read_text() == 'first'
    call(folder, 'delete', 'notes/renamed.md')
    call(folder, 'delete', 'notes/two.md')
    call(folder, 'delete', 'notes')
    assert not (root / 'notes').exists()


def test_save_commit_as_person_only_selected_file_no_hooks_or_push(folder, tmp_path):
    _, root = folder
    repo(root)
    remote = tmp_path / 'remote.git'
    git(tmp_path, 'init', '--bare', str(remote))
    git(root, 'remote', 'add', 'origin', str(remote))
    (root / 'other.txt').write_text('not this commit')
    git(root, 'add', 'other.txt')
    marker = tmp_path / 'hook-ran'
    hook = root / '.git/hooks/pre-commit'
    hook.write_text(f'#!/bin/sh\ntouch {marker}\n')
    hook.chmod(0o755)
    opened = call(folder, 'read', 'app.py')
    result = call(folder, 'write', 'app.py', content='edited', revision=opened['revision'],
                  message='Edit app', name='Alex Example', email='alex@example.com')
    assert result['commit']
    log = subprocess.check_output(['git', '-C', str(root), 'log', '-1', '--format=%an <%ae>|%cn <%ce>']).decode().strip()
    assert log == 'Alex Example <alex@example.com>|Alex Example <alex@example.com>'
    assert subprocess.check_output(['git', '-C', str(root), 'show', '--format=', '--name-only']).decode().strip() == 'app.py'
    assert 'other.txt' in subprocess.check_output(['git', '-C', str(root), 'diff', '--cached', '--name-only']).decode()
    assert not marker.exists()
    assert not list((remote / 'refs/heads').iterdir())


async def test_api_redacts_reads_conflicts_downloads_and_audits_paths_only(api, project, env):
    from sqlalchemy import select

    from acc.server.models import AuditLog, VaultGrant
    from acc.server.vault import create_key
    root = env['workspaces'] / 'demo'
    secret = 'example-sensitive-value-12345'
    with write_db(api.app.state.db) as s:
        key = create_key(s, api.app.state.supervisor.vault, name='Demo key', variable='DEMO_KEY', value=secret, owner='@api')
        s.flush()
        s.add(VaultGrant(key_id=key.id, project_id=project['id'], given_by='@api'))
        s.commit()
    (root / 'notes.md').write_text('# ' + secret)
    url = f"/api/files/project/{project['id']}"
    opened = await api.get(url + '/read', params={'path': 'notes.md'})
    assert opened.status_code == 200, opened.text
    assert secret not in opened.text and opened.json()['redacted']
    refused = await api.post(url, json={'action': 'write', 'path': 'notes.md', 'content': 'masked', 'revision': opened.json()['revision']})
    assert refused.status_code == 409
    downloaded = await api.get(url + '/download', params={'path': 'notes.md'})
    assert downloaded.status_code == 200 and secret not in downloaded.text
    (root / 'notes.md').write_text('plain')
    opened = (await api.get(url + '/read', params={'path': 'notes.md'})).json()
    (root / 'notes.md').write_text('changed ' + secret)
    conflict = await api.post(url, json={'action': 'write', 'path': 'notes.md', 'content': 'draft', 'revision': opened['revision']})
    assert conflict.status_code == 200 and conflict.json()['conflict'] and secret not in conflict.text
    (root / 'notes.md').write_text('plain')
    opened = (await api.get(url + '/read', params={'path': 'notes.md'})).json()
    saved = await api.post(url, json={'action': 'write', 'path': 'notes.md', 'content': 'my edited content', 'revision': opened['revision']})
    assert saved.status_code == 200 and (root / 'notes.md').read_text() == 'my edited content'
    with api.app.state.db() as s:
        audit = list(s.scalars(select(AuditLog).where(AuditLog.action == 'files.write')))
        assert len(audit) == 1 and audit[0].detail['paths'] == ['notes.md']
        assert 'my edited content' not in str(audit[0].detail)


async def test_account_commit_identity_is_personal_and_required(api, env, project):
    from test_auth import do_setup
    from test_users import browser, csrf, join
    await do_setup(api, env)
    repo(env['workspaces'] / 'demo')
    async with browser(api) as person:
        # Set up a second owner with their own stored identity.
        link = (await verified_post(api, '/api/users/invites', {'role': 'owner'})).json()['link']
        assert (await join(person, link, 'alex')).status_code == 200
        url = f"/api/files/project/{project['id']}"
        opened = (await person.get(url + '/read', params={'path': 'app.py'})).json()
        body = {'action': 'write', 'path': 'app.py', 'content': 'edit', 'revision': opened['revision'], 'message': 'My commit'}
        assert (await person.post(url, json=body, headers=csrf(person))).status_code == 422
        assert (env['workspaces'] / 'demo/app.py').read_text() == 'one\ntwo\n'
        identity = await person.post('/api/files/identity', json={'name': 'Alex Example', 'email': 'alex@example.com'}, headers=csrf(person))
        assert identity.status_code == 200
        assert (await api.get('/api/files/identity')).json() == {'name': '', 'email': ''}
        saved = await person.post(url, json=body, headers=csrf(person))
        assert saved.status_code == 200 and saved.json()['commit']
        audit = (await api.get('/api/audit?action=files.commit')).json()
        assert audit[0]['actor'] == 'alex' and audit[0]['detail']['paths'] == ['app.py']


async def test_files_permissions_and_session_worktree(api, env, project, member_accounts):
    from test_auth import do_setup
    from test_users import browser, csrf, join

    await do_setup(api, env)
    (env['workspaces'] / 'demo/owner.md').write_text('owner')
    async with browser(api) as member:
        link = (await api.post('/api/users/invites', json={'role': 'member', 'account': 'dana-agent'})).json()['link']
        await join(member, link, 'dana')
        other = f"/api/files/project/{project['id']}"
        for suffix in ['', '/read', '/download']:
            assert (await member.get(other + suffix, params={'path': 'owner.md'})).status_code == 404
        assert (await member.post(other, json={'action': 'delete', 'path': 'owner.md'}, headers=csrf(member))).status_code == 404
        mine = (await member.post('/api/projects', json={'name': 'Mine', 'slug': 'mine', 'workspace_path': str(env['workspaces'] / 'demo')}, headers=csrf(member))).json()
        url = f"/api/files/project/{mine['id']}"
        assert (await member.get(url)).status_code == 200
        assert (await member.post(url, json={'action': 'create', 'path': 'mine.md', 'content': 'mine'}, headers=csrf(member))).status_code == 200
        assert (await api.get(url + '/read', params={'path': 'mine.md'})).json()['content'] == 'mine'
        # Separate session checkout: do not serve the project's main checkout by accident.
        repo(env['workspaces'] / 'demo')
        await member.post(f"/api/projects/{mine['id']}/trust", headers=csrf(member))
        made = (await member.post('/api/sessions', json={'project_id': mine['id'], 'name': 'Tree', 'provider': 'claude', 'worktree': True}, headers=csrf(member))).json()
        from helpers import wait_for
        from test_supervisor import state_is
        await wait_for(state_is(api, made['id'], 'idle'))
        # Use a real launch placement rather than changing routing after launch.
        (Path(made['worktree']) / 'session.md').write_text('worktree')
        session = '/api/files/session/' + made['id']
        assert (await member.get(session + '/read', params={'path': 'session.md'})).json()['content'] == 'worktree'
        assert (await member.get(session)).json()['working']
        # Another member cannot read the first member's project or session.
        async with browser(api) as other_member:
            link = (await api.post('/api/users/invites', json={'role': 'member', 'account': 'alice-agent'})).json()['link']
            await join(other_member, link, 'blair')
            assert (await other_member.get(url)).status_code == 404
            assert (await other_member.get(session)).status_code == 404


async def test_files_remote_operations_use_real_machine_link(api, env, live):  # noqa: F811
    import asyncio

    from helpers import wait_for
    from test_multihost import StandInNode, enroll
    key, node_id = await enroll(api)
    node = StandInNode(env['settings'].broker_socket, key, node_id)
    task = asyncio.create_task(node.run(live))
    try:
        await wait_for(lambda: _connected(api))
        root = env['workspaces'] / 'remote'
        root.mkdir()
        project = (await api.post('/api/projects', json={'slug': 'remote', 'name': 'Remote', 'host': 'pc', 'workspace_path': str(root)})).json()
        url = f"/api/files/project/{project['id']}"
        assert (await api.post(url, json={'action': 'create', 'path': 'remote.md', 'content': '# Remote'})).status_code == 200
        assert (await api.get(url + '/read', params={'path': 'remote.md'})).json()['content'] == '# Remote'
        assert (await api.get(url)).json()['entries'][0]['name'] == 'remote.md'
        assert node.relayed >= 3 and node.opened == ['agent']
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def _connected(api):
    return any(n['connected'] for n in (await api.get('/api/nodes')).json())

from test_auth import live  # noqa: E402,F401


def test_rename_populated_folder_preserves_contents_and_refuses_secret_descendants(folder):
    _, root = folder
    (root / 'docs').mkdir()
    (root / 'docs/note.md').write_text('keep this')
    call(folder, 'rename', 'docs', destination='notes')
    assert (root / 'notes/note.md').read_text() == 'keep this'
    assert not (root / 'docs').exists()
    (root / 'notes/.env').write_text('secret')
    with pytest.raises(ops.OpError):
        call(folder, 'rename', 'notes', destination='moved')
    assert (root / 'notes/.env').read_text() == 'secret'


def test_guard_secret_policy_parity_and_run_folder(folder):
    import runpy
    from pathlib import Path

    from acc.broker.files import protected
    guard = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'bin/acc-guard'))
    config, root = folder
    samples = ['.env', '.env.local', '.env.example', '.env.sample', '.env.template', '.env.dist', '.env.defaults',
               'session.env', '.netrc', '.pgpass', '.git-credentials', 'id_rsa', 'id_dsa', 'id_ecdsa', 'id_ed25519',
               # Path names only; split the inventory so it cannot resemble a key assignment.
               'key.pem',
               'key.p12',
               'key.pfx',
               'id_ed25519.pub',
               'credentials.json',
               'service-account-demo.json',
               '.ssh/key', '.gnupg/key', '.aws/credentials', '.docker/config.json', '.config/gh/hosts.yml',
               *[p.lstrip('/') for p in guard['SECRET_FILES']]]
    for rel in samples:
        assert protected(config, root, rel) == guard['secret_path'](str(root / rel)), rel
    config.run_dir.mkdir()
    (config.run_dir / 'mcp.json').write_text('never shown')
    with pytest.raises(ops.OpError):
        ops.files(config, str(config.run_dir), action='list')
    (root / 'runlink').symlink_to(config.run_dir, target_is_directory=True)
    with pytest.raises(ops.OpError):
        call(folder, 'read', 'runlink/mcp.json')


async def test_binary_redaction_and_mutation_audits(api, env, project):
    from sqlalchemy import select

    from acc.server.models import AuditLog
    from acc.server.vault import create_key
    secret = 'example-binary-secret-12345'
    root = env['workspaces'] / 'demo'
    with write_db(api.app.state.db) as s:
        create_key(s, api.app.state.supervisor.vault, name='Binary key', variable='BINARY_KEY', value=secret, owner='@api')
        s.commit()
    (root / 'blob.bin').write_bytes(b'\0' + secret.encode())
    url = f"/api/files/project/{project['id']}"
    assert (await api.get(url + '/download', params={'path': 'blob.bin'})).status_code == 409
    assert (await api.post(url, json={'action': 'mkdir', 'path': 'docs'})).status_code == 200
    assert (await api.post(url, json={'action': 'create', 'path': 'docs/one', 'content': 'not in audit'})).status_code == 200
    assert (await api.post(url, json={'action': 'rename', 'path': 'docs/one', 'destination': 'docs/two'})).status_code == 200
    assert (await api.post(url, json={'action': 'delete', 'path': 'docs/two'})).status_code == 200
    with api.app.state.db() as s:
        rows = list(s.scalars(select(AuditLog).where(AuditLog.action.like('files.%')).order_by(AuditLog.id)))
        assert [r.action for r in rows] == ['files.write', 'files.write', 'files.write', 'files.delete']
        assert rows[2].detail['paths'] == ['docs/one', 'docs/two']
        assert 'not in audit' not in str([r.detail for r in rows])


async def test_binary_download_refuses_truncated_vault_values(api, env, project):
    from acc.server.vault import create_key
    secret = 'example-long-secret-value-for-prefix-test'
    with write_db(api.app.state.db) as s:
        create_key(s, api.app.state.supervisor.vault, name='Prefix', variable='PREFIX_KEY', value=secret, owner='@api')
        s.commit()
    (env['workspaces'] / 'demo/prefix.bin').write_bytes(b'\0' + secret[:25].encode())
    response = await api.get(f"/api/files/project/{project['id']}/download", params={'path': 'prefix.bin'})
    assert response.status_code == 409


def test_commit_is_reported_when_only_real_index_refresh_is_locked(folder):
    _, root = folder
    repo(root)
    (root / '.git/index.lock').write_text('another git process owns the index')
    opened = call(folder, 'read', 'app.py')
    result = call(folder, 'write', 'app.py', content='committed bytes', revision=opened['revision'],
                  message='Edit', name='Alex Example', email='alex@example.com')
    assert result.get('commit') and result.get('commit_warning')
    assert subprocess.check_output(['git', '-C', str(root), 'show', 'HEAD:app.py']) == b'committed bytes'


def test_folder_rename_cannot_create_protected_descendant_at_destination(folder):
    _, root = folder
    (root / 'settings').mkdir()
    (root / 'settings/auth.json').write_text('cannot become CLI credentials')
    with pytest.raises(ops.OpError):
        call(folder, 'rename', 'settings', destination='.codex')
    assert (root / 'settings/auth.json').exists() and not (root / '.codex').exists()


async def test_local_git_identity_create_and_update_are_audited_without_personal_details(api):
    for name, email in [("Alex Example", "alex@example.com"), ("Blair Example", "blair@example.com")]:
        response = await api.post("/api/files/identity", json={"name": name, "email": email})
        assert response.status_code == 200
    assert (await api.post("/api/files/identity", json={"name": "invalid", "email": "bad"})).status_code == 422
    entries = (await api.get("/api/audit?action=git.identity.update")).json()
    assert len(entries) == 2
    assert all(entry["actor"] == "@api" and entry["target"] == "@api" and entry["detail"] == {} for entry in entries)
    assert "Example" not in str(entries) and "example.com" not in str(entries)
    assert (await api.get("/api/files/identity")).json() == {"name": "Blair Example", "email": "blair@example.com"}


@pytest.mark.parametrize(('name', 'fallback', 'encoded'), [
    ('report.txt', 'report.txt', 'report.txt'),
    ('my report.txt', 'my report.txt', 'my%20report.txt'),
    ('résumé.txt', 'rsum.txt', 'r%C3%A9sum%C3%A9.txt'),
    ('say"hello.txt', 'sayhello.txt', 'say%22hello.txt'),
    ('odd∕name\r\n.txt', 'oddname.txt', 'odd%E2%88%95name%0D%0A.txt'),
])
async def test_download_preserves_basename_without_header_injection(api, project, env, name, fallback, encoded):
    root = env['workspaces'] / 'demo'
    (root / 'nested').mkdir()
    (root / 'nested' / name).write_text('download contents')
    response = await api.get(f"/api/files/project/{project['id']}/download", params={'path': 'nested/' + name})
    assert response.status_code == 200, response.text
    assert response.headers['content-disposition'] == f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{encoded}'
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert response.headers['content-security-policy'] == "sandbox; default-src 'none'"
    assert response.headers['cache-control'] == 'no-store'
    assert response.text == 'download contents'


@pytest.mark.parametrize(('data', 'media'), [
    (b'ID3\x04\0\0\0\0\0\0', 'audio/mpeg'),
    (b'RIFF\0\0\0\0WAVEfmt ', 'audio/wav'),
    (b'OggS\0\x02\0\0', 'audio/ogg'),
    (b'\0\0\0\x18ftypM4A \0\0\0\0', 'audio/mp4'),
    (b'\0\0\0\x18ftypisom\0\0\0\0', 'video/mp4'),
    (b'\x1a\x45\xdf\xa3\x87\x42\x82\x84webm', 'video/webm'),
])
def test_safe_media_is_identified_for_read_and_download(folder, data, media):
    _, root = folder
    (root / 'media').write_bytes(data)
    for action in ('read', 'download'):
        result = call(folder, action, 'media')
        assert result['binary']
        assert result['media_type'] == media


@pytest.mark.parametrize('data', [b'<html>hello</html>', b'<svg xmlns="http://www.w3.org/2000/svg"/>', b'\0unknown'])
def test_active_or_unknown_files_never_get_inline_media_types(folder, data):
    _, root = folder
    (root / 'pretend.mp3').write_bytes(data)
    assert not call(folder, 'read', 'pretend.mp3').get('media_type')


def test_download_header_fallback_removes_backslashes_and_controls():
    from acc.server.api.files import download_disposition
    assert download_disposition('nested/a\\b\x00\x1f\x7f.txt') == (
        'attachment; filename="ab.txt"; filename*=UTF-8\'\'a%5Cb%00%1F%7F.txt')
    assert download_disposition('résumé/文') == 'attachment; filename="download"; filename*=UTF-8\'\'%E6%96%87'
