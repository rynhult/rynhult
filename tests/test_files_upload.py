"""Binary uploads retain Files descriptor, protection and revision boundaries."""
import base64

import pytest
from isolated_settings import isolated_broker
from test_files import call


@pytest.fixture
def folder(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    return isolated_broker(tmp_path, allowed_roots=(tmp_path,), run_dir=tmp_path / 'run'), root


def upload(folder, path, raw=b'\x00binary', **kwargs):
    return call(folder, 'upload', path, data=base64.b64encode(raw).decode(), **kwargs)


def test_upload_binary_conflict_replace_and_keep_both(folder):
    _, root = folder
    first = upload(folder, 'voice.wav')
    assert (root / 'voice.wav').read_bytes() == b'\x00binary'
    conflict = upload(folder, 'voice.wav', b'new')
    assert conflict['conflict'] and conflict['revision'] == first['revision']
    assert (root / 'voice.wav').read_bytes() == b'\x00binary'
    (root / 'voice.wav').write_bytes(b'changed elsewhere')
    stale = upload(folder, 'voice.wav', b'new', overwrite='replace', revision=first['revision'])
    assert stale['conflict']
    kept = upload(folder, 'voice.wav', b'kept', overwrite='keep-both')
    assert kept['path'] == 'voice (1).wav'
    assert (root / kept['path']).read_bytes() == b'kept'
    replaced = upload(folder, 'voice.wav', b'replaced', overwrite='replace', revision=stale['revision'])
    assert not replaced.get('conflict') and (root / 'voice.wav').read_bytes() == b'replaced'


@pytest.mark.parametrize('path', ['../escape', '/tmp/escape', '.git/config', '.env', 'sub/../../escape', 'a\\b'])
def test_upload_refuses_traversal_and_protected_paths(folder, path):
    from acc.broker.ops import OpError
    with pytest.raises(OpError):
        upload(folder, path)


def test_upload_refuses_symlinks_and_oversize(folder, tmp_path, monkeypatch):
    from acc.broker import files
    from acc.broker.ops import OpError
    _, root = folder
    target = tmp_path / 'outside'
    target.mkdir()
    (root / 'link').symlink_to(target, target_is_directory=True)
    (root / 'file-link').symlink_to(target / 'missing')
    for name in ['link/escape', 'file-link']:
        with pytest.raises((OpError, OSError)):
            upload(folder, name)
    monkeypatch.setattr(files, 'MAX_UPLOAD', 2)
    with pytest.raises(OpError, match='limit'):
        upload(folder, 'large', b'123')
    assert not (root / 'large').exists()


def batch(path, raw=b'\0binary', **kwargs):
    return {'files': [{'path': path, 'data': base64.b64encode(raw).decode(), **kwargs}]}


async def test_upload_api_batch_audit_limits_and_secret_replacement(api, env, project, monkeypatch):
    from helpers import write_db
    from sqlalchemy import select

    from acc.server.api import files
    from acc.server.models import AuditLog
    from acc.server.vault import create_key

    url = f"/api/files/project/{project['id']}/upload"
    root = env['workspaces'] / 'demo'
    response = await api.post(url, json={'files': [*batch('voice.wav')['files'], *batch('.git/config')['files'],
                                                   {'path': 'bad.bin', 'data': 'not-base64'}]})
    assert response.status_code == 200
    rows = response.json()['files']
    assert rows[0]['path'] == 'voice.wav'
    assert rows[1]['error'] and rows[2]['error']
    assert (root / 'voice.wav').read_bytes() == b'\0binary'
    with api.app.state.db() as s:
        audits = list(s.scalars(select(AuditLog).where(AuditLog.action == 'files.upload')))
        assert len(audits) == 1 and len(audits[0].detail['files']) == 3
        assert 'AGJpbmFyeQ==' not in str(audits[0].detail)
    monkeypatch.setattr(files, 'MAX_UPLOAD_BATCH', 2)
    assert (await api.post(url, json=batch('large.bin', b'123'))).status_code == 413
    assert not (root / 'large.bin').exists()
    monkeypatch.setattr(files, 'MAX_UPLOAD_BATCH', 16 << 20)
    monkeypatch.setattr(files, 'MAX_UPLOAD', 2)
    refused = (await api.post(url, json=batch('large.bin', b'123'))).json()
    assert 'limit' in refused['files'][0]['error']
    monkeypatch.setattr(files, 'MAX_UPLOAD', 4 << 20)
    secret = 'example-upload-hidden-key-12345678'
    with write_db(api.app.state.db) as s:
        create_key(s, api.app.state.supervisor.vault, name='Upload key', variable='UPLOAD_KEY', value=secret, owner='@api')
        s.commit()
    (root / 'hidden.bin').write_bytes(b'\0' + secret.encode())
    conflict = (await api.post(url, json=batch('hidden.bin'))).json()['files'][0]
    refused = (await api.post(url, json=batch('hidden.bin', b'replace', overwrite='replace', revision=conflict['revision']))).json()
    assert 'hidden secret' in refused['files'][0]['error']
    assert (root / 'hidden.bin').read_bytes() == b'\0' + secret.encode()


async def test_upload_api_member_owner_and_workspace_routing(api, env, project, member_accounts):
    from test_auth import do_setup
    from test_users import browser, csrf, join
    await do_setup(api, env)
    async with browser(api) as member:
        link = (await api.post('/api/users/invites', json={'role': 'member', 'account': 'dana-agent'})).json()['link']
        await join(member, link, 'dana')
        assert (await member.post(f"/api/files/project/{project['id']}/upload", json=batch('no'), headers=csrf(member))).status_code == 404
        mine = (await member.post('/api/projects', json={'name': 'Mine', 'slug': 'mine', 'workspace_path': str(env['workspaces'] / 'demo')}, headers=csrf(member))).json()
        url = f"/api/files/project/{mine['id']}/upload"
        assert (await member.post(url, json=batch('mine.bin'), headers=csrf(member))).json()['files'][0]['path'] == 'mine.bin'
        assert (await api.post(url, json=batch('owner.bin'))).json()['files'][0]['path'] == 'owner.bin'
        for kind in ['session', 'workspace']:
            assert (await member.post(f'/api/files/{kind}/missing/upload', json=batch('no'), headers=csrf(member))).status_code == 404
        from pathlib import Path

        from helpers import wait_for
        from test_changes import repo
        from test_supervisor import state_is
        repo(env['workspaces'] / 'demo')
        await member.post(f"/api/projects/{mine['id']}/trust", headers=csrf(member))
        launched = await member.post('/api/sessions', json={'project_id': mine['id'], 'provider': 'claude',
                                                          'name': 'Upload worktree', 'worktree': True}, headers=csrf(member))
        assert launched.status_code == 201
        session = launched.json()
        await wait_for(state_is(api, session['id'], 'idle'))
        for kind in ['session', 'workspace']:
            route = f"/api/files/{kind}/{session['id']}/upload"
            assert (await member.post(route, json=batch(f'{kind}.bin'), headers=csrf(member))).json()['files'][0]['path'] == f'{kind}.bin'
            assert (Path(session['worktree']) / f'{kind}.bin').exists()
            assert not (env['workspaces'] / 'demo' / f'{kind}.bin').exists()
        async with browser(api) as other:
            link = (await api.post('/api/users/invites', json={'role': 'member', 'account': 'alice-agent'})).json()['link']
            await join(other, link, 'blair')
            for kind in ['session', 'workspace']:
                route = f"/api/files/{kind}/{session['id']}/upload"
                assert (await other.post(route, json=batch('no'), headers=csrf(other))).status_code == 404


def test_upload_rechecks_revision_after_staging_and_cleans_temps(folder, monkeypatch):
    from acc.broker import files
    _, root = folder
    first = upload(folder, 'race.bin', b'initial')
    fsync = files.os.fsync
    def changed(fd):
        (root / 'race.bin').write_bytes(b'concurrent writer')
        return fsync(fd)
    monkeypatch.setattr(files.os, 'fsync', changed)
    result = upload(folder, 'race.bin', b'ours', overwrite='replace', revision=first['revision'])
    assert result['conflict'] and (root / 'race.bin').read_bytes() == b'concurrent writer'
    assert not list(root.glob('.acc-save-*'))


def test_upload_concurrent_creates_never_overwrite(folder):
    from concurrent.futures import ThreadPoolExecutor
    _, root = folder
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda value: upload(folder, 'same.bin', value), [b'one', b'two']))
    assert sum(bool(row.get('conflict')) for row in results) == 1
    assert (root / 'same.bin').read_bytes() in (b'one', b'two')


def test_upload_encoding_and_conflict_choices_are_bounded(folder):
    from acc.broker.ops import OpError
    for kwargs in [{'data': '!bad'}, {'data': '', 'overwrite': 'force'}, {'data': '', 'overwrite': 'replace'}]:
        with pytest.raises(OpError):
            call(folder, 'upload', 'new.bin', **kwargs)


async def test_upload_full_size_binary_and_explicit_size_refusal(api, env, project):
    url = f"/api/files/project/{project['id']}/upload"
    raw = b'\0' * (4 << 20)
    result = await api.post(url, json=batch('full.bin', raw))
    assert result.status_code == 200 and result.json()['files'][0]['size'] == len(raw)
    assert (env['workspaces'] / 'demo/full.bin').read_bytes() == raw
    response = await api.post(url, json=batch('too-big.bin', raw + b'xx'))
    assert response.status_code == 200 and '4 MiB per-file limit' in response.json()['files'][0]['error']
    assert not (env['workspaces'] / 'demo/too-big.bin').exists()


async def test_upload_body_limit_is_scoped_to_upload_route(api, project):
    base = f"/api/files/project/{project['id']}"
    for path, declared in [(base + '/upload', 25 << 20), (base, 5 << 20), (base + '/upload-extra', 5 << 20)]:
        response = await api.post(path, content=b'{}', headers={'Content-Length': str(declared), 'Content-Type': 'application/json'})
        assert response.status_code == 413
