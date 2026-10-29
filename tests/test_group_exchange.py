"""Authorization is rechecked at consumption, not just when minting a capability."""
# ruff: noqa: F811
import base64

import pytest
from helpers import write_db
from test_auth import live  # noqa: F401
from test_gitbackup import git, make_repo
from test_groups import grouped, two  # noqa: F401
from test_users import csrf

from acc.server.models import Group, GroupMembership, Placement


@pytest.fixture
async def exchange_case(grouped, tmp_path):
    owner = grouped['owner']
    base = f"/api/groups/{grouped['group']['id']}/projects/{grouped['group_project']['id']}"
    result = await owner.post(base + '/placements', json={'user_id': 2, 'revision': 2})
    assert result.status_code == 201
    source = make_repo(tmp_path / 'source')
    sha = git(source, 'rev-parse', 'HEAD').stdout.strip()
    return {**grouped, 'base': base, 'placement': result.json()['id'], 'source': source, 'sha': sha}


async def grant(case, operation='publish', **overrides):
    client = case['member']
    return await client.post(case['base'] + '/exchange/grants', json={
        'operation': operation, 'placement_id': case['placement'] if operation == 'publish' else None,
        'session_id': 'a' * 32, 'old_sha': '0' * 40, 'new_sha': case['sha'], **overrides}, headers=csrf(client))


async def transfer(case, token, data=None):
    return await case['member'].post(case['base'] + '/exchange/transfer',
        headers={'Authorization': 'Bearer ' + token}, json={'data': data})


async def test_publish_fetch_replay_and_named_owner_integration(exchange_case):
    from acc import git_exchange
    case = exchange_case
    minted = await grant(case)
    assert minted.status_code == 201, minted.text
    pack = base64.b64encode(git_exchange.export(case['source'] / '.git', case['sha'])).decode()
    token = minted.json()['token']
    accepted = await transfer(case, token, pack)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()['ref'].startswith('refs/heads/people/')
    assert (await transfer(case, token, pack)).status_code == 401
    refs = await case['member'].get(case['base'] + '/exchange/refs')
    assert list(refs.json().values()) == [case['sha']]
    fetched = await grant(case, 'fetch')
    received = await transfer(case, fetched.json()['token'])
    assert received.status_code == 200
    restored = git_exchange.initialize(case['source'].parent / 'restored', 'restored')
    git_exchange.publish(restored, base64.b64decode(received.json()['data']), 'refs/heads/restored', '0' * 40, case['sha'])
    assert git_exchange.read_file(restored, case['sha'], 'README.md') == b'hello\n'
    assert (await case['member'].get(case['base'] + '/published/file', params={'commit': case['sha'], 'path': 'README.md'})).json()['content'] == 'hello\n'
    approved = await case['member'].post(case['base'] + '/approvals', json={
        'revision': 3, 'branch': 'main', 'old_sha': '0' * 40, 'new_sha': case['sha']}, headers=csrf(case['member']))
    assert approved.status_code == 201
    integrated = await case['member'].post(case['base'] + '/exchange/integrate', json={'approval_id': approved.json()['id']}, headers=csrf(case['member']))
    assert integrated.status_code == 200, integrated.text
    assert (await case['member'].post(case['base'] + '/exchange/integrate', json={'approval_id': approved.json()['id']}, headers=csrf(case['member']))).status_code == 409
    # A delayed apply must not select a newer receipt after SQLite recycles ids.
    later = await case['member'].post(case['base'] + '/approvals', json={
        'revision': 4, 'branch': 'develop', 'old_sha': '0' * 40, 'new_sha': case['sha']}, headers=csrf(case['member']))
    assert later.status_code == 201
    stale_apply = await case['member'].post(case['base'] + '/exchange/integrate', json={'approval_id': approved.json()['id']}, headers=csrf(case['member']))
    assert stale_apply.status_code == 409
    assert 'refs/heads/develop' not in (await case['member'].get(case['base'] + '/exchange/refs')).json()


async def test_removed_and_viewer_roles_invalidate_minted_write_grants(exchange_case):
    case = exchange_case
    minted = await grant(case)
    assert minted.status_code == 201, minted.text
    with write_db(case['owner'].app.state.db) as s:
        s.get(GroupMembership, (case['group']['id'], 2)).role = 'viewer'
        s.get(Group, case['group']['id']).revision += 1
        s.commit()
    assert (await transfer(case, minted.json()['token'])).status_code == 404
    assert (await grant(case)).status_code == 404
    assert (await grant(case, 'fetch')).status_code == 201
    with write_db(case['owner'].app.state.db) as s:
        s.delete(s.get(GroupMembership, (case['group']['id'], 2)))
        s.get(Group, case['group']['id']).revision += 1
        s.commit()
    assert (await grant(case, 'fetch')).status_code == 404
    assert (await case['member'].get(case['base'] + '/exchange/refs')).status_code == 404


async def test_grant_cannot_bind_another_person_or_project(exchange_case):
    case = exchange_case
    with write_db(case['owner'].app.state.db) as s:
        p = s.get(Placement, case['placement'])
        p.person = 'casey'
        s.commit()
    assert (await grant(case)).status_code == 404
    assert (await grant(case, 'fetch', placement_id=case['placement'])).status_code == 404


async def test_published_tree_wiki_backups_restore_and_viewer_fetch(exchange_case):
    from acc import git_exchange
    case = exchange_case
    source = case['source']
    (source / 'docs/wiki').mkdir(parents=True)
    (source / 'docs/wiki/Home.md').write_text('Published guide')
    (source / 'outside').symlink_to('/etc/passwd')
    from test_gitbackup import commit
    case['sha'] = commit(source)
    minted = await grant(case)
    pack = base64.b64encode(git_exchange.export(source / '.git', case['sha'])).decode()
    assert (await transfer(case, minted.json()['token'], pack)).status_code == 200
    tree = await case['member'].get(case['base'] + '/published/tree', params={'commit': case['sha']})
    assert tree.status_code == 200, tree.text
    assert 'docs/wiki/Home.md' in tree.json()
    assert 'outside' not in tree.json()
    page = await case['member'].get(case['base'] + '/published/wiki', params={'commit': case['sha'], 'path': 'Home.md'})
    assert page.json()['content'] == 'Published guide'
    backed = await case['member'].post(case['base'] + '/exchange/backup', headers=csrf(case['member']))
    assert backed.status_code == 201, backed.text
    snapshot = backed.json()['snapshot']
    restored = await case['member'].post(case['base'] + '/exchange/restore', json={'snapshot': snapshot, 'commit': case['sha']}, headers=csrf(case['member']))
    assert restored.status_code == 200, restored.text
    assert restored.json()['ref'].startswith('refs/heads/restore/')
    with write_db(case['owner'].app.state.db) as s:
        s.get(GroupMembership, (case['group']['id'], 2)).role = 'viewer'
        s.get(Group, case['group']['id']).revision += 1
        s.commit()
    fetched = await grant(case, 'fetch')
    assert (await transfer(case, fetched.json()['token'])).status_code == 200
    assert (await case['member'].post(case['base'] + '/exchange/backup', headers=csrf(case['member']))).status_code == 404
    assert (await case['member'].get(case['base'] + '/exchange/backups')).status_code == 200
    assert (await case['member'].get(case['base'] + '/exchange/backup/' + snapshot)).status_code == 404


async def test_competing_publications_and_stale_approval_do_not_overwrite(exchange_case):
    import asyncio

    from acc import git_exchange
    case = exchange_case
    grants = [await grant(case) for _ in range(2)]
    pack = base64.b64encode(git_exchange.export(case['source'] / '.git', case['sha'])).decode()
    results = await asyncio.gather(*(transfer(case, g.json()['token'], pack) for g in grants))
    assert sorted(r.status_code for r in results) == [200, 409]
    approval = await case['member'].post(case['base'] + '/approvals', json={
        'revision': 3, 'branch': 'main', 'old_sha': '0' * 40, 'new_sha': case['sha']}, headers=csrf(case['member']))
    assert approval.status_code == 201
    assert (await case['owner'].post(f"/api/groups/{case['group']['id']}/members/1", json={'revision': 4, 'role': 'viewer'})).status_code == 200
    integrated = await case['member'].post(case['base'] + '/exchange/integrate', json={'approval_id': approval.json()['id']}, headers=csrf(case['member']))
    assert integrated.status_code == 409
    assert 'refs/heads/main' not in (await case['member'].get(case['base'] + '/exchange/refs')).json()
    tagged = await case['member'].post(case['base'] + '/exchange/tags', json={'name': 'v1', 'commit': case['sha']}, headers=csrf(case['member']))
    assert tagged.status_code == 201, tagged.text
    assert (await case['member'].post(case['base'] + '/exchange/tags', json={'name': 'v1', 'commit': case['sha']}, headers=csrf(case['member']))).status_code == 409


async def test_reserved_placement_provision_and_checkout_use_its_broker(exchange_case):
    from acc import git_exchange
    case = exchange_case
    endpoint = case['base'] + f"/placements/{case['placement']}/provision"
    assert (await case['member'].post(endpoint, headers=csrf(case['member']))).status_code == 403
    made = await case['owner'].post(endpoint)
    assert made.status_code == 200, made.text
    assert made.json()['sync_status'] == 'ready'
    minted = await grant(case)
    pack = base64.b64encode(git_exchange.export(case['source'] / '.git', case['sha'])).decode()
    assert (await transfer(case, minted.json()['token'], pack)).status_code == 200
    checked = await case['member'].post(case['base'] + '/exchange/checkout', json={
        'placement_id': case['placement'], 'session_id': 'b' * 12, 'commit': case['sha']}, headers=csrf(case['member']))
    assert checked.status_code == 200, checked.text
    from pathlib import Path
    path = Path(checked.json()['path'])
    assert (path / 'README.md').read_text() == 'hello\n'
    (path / 'README.md').write_text('dirty-private-canary')
    repeated = await case['member'].post(case['base'] + '/exchange/checkout', json={
        'placement_id': case['placement'], 'session_id': 'b' * 12, 'commit': case['sha']}, headers=csrf(case['member']))
    assert repeated.status_code == 200, repeated.text
    assert (path / 'README.md').read_text() == 'dirty-private-canary'
    published = await case['member'].post(case['base'] + '/exchange/publish', json={
        'placement_id': case['placement'], 'session_id': 'b' * 12, 'new_sha': case['sha'], 'old_sha': '0' * 40}, headers=csrf(case['member']))
    assert published.status_code == 200, published.text
    assert 'dirty-private-canary' not in (await case['member'].get(case['base'] + '/published/file', params={'commit': case['sha'], 'path': 'README.md'})).text


async def test_private_backup_never_enters_group_recovery_and_survives_membership_removal(exchange_case):
    from pathlib import Path

    from test_gitbackup import commit

    from acc import git_exchange
    case = exchange_case
    assert (await case['owner'].post(case['base'] + f"/placements/{case['placement']}/provision")).status_code == 200
    minted = await grant(case)
    pack = base64.b64encode(git_exchange.export(case['source'] / '.git', case['sha'])).decode()
    assert (await transfer(case, minted.json()['token'], pack)).status_code == 200
    checked = await case['member'].post(case['base'] + '/exchange/checkout', json={
        'placement_id': case['placement'], 'session_id': 'b' * 12, 'commit': case['sha']}, headers=csrf(case['member']))
    path = Path(checked.json()['path'])
    git(path, 'config', 'user.name', 'Alex Example')
    git(path, 'config', 'user.email', 'alex@example.com')
    private = commit(path, 'private.txt', 'unpublished-private-recovery-canary')
    endpoint = case['base'] + f"/placements/{case['placement']}/private-backups"
    saved = await case['member'].post(endpoint, json={'session_id': 'b' * 12, 'commit': private}, headers=csrf(case['member']))
    assert saved.status_code == 201, saved.text
    own = await case['member'].get(endpoint + '/' + saved.json()['snapshot'])
    assert own.status_code == 200
    stage = git_exchange.initialize(path.parent / 'private-restore', 'private')
    git_exchange.publish(stage, base64.b64decode(own.json()['data']), 'refs/heads/restored', '0' * 40, private)
    assert git_exchange.read_file(stage, private, 'private.txt') == b'unpublished-private-recovery-canary'
    group_backup = await case['member'].post(case['base'] + '/exchange/backup', headers=csrf(case['member']))
    group_saved = await case['member'].get(case['base'] + '/exchange/backup/' + group_backup.json()['snapshot'])
    assert private not in group_saved.json()['packs']
    from test_users import browser

    from acc.server import auth
    from acc.server.models import User
    with write_db(case['owner'].app.state.db) as s:
        teammate = User(username='arlen', role='member', password_hash='unused', totp_secret='unused')
        s.add(teammate)
        s.flush()
        s.add(GroupMembership(group_id=case['group']['id'], user_id=teammate.id, role='maintainer'))
        s.get(Group, case['group']['id']).revision += 1
        token, csrf_token = auth.create_session(s, teammate, 'test')
        s.commit()
    async with browser(case['owner']) as other:
        other.cookies.set('acc_session', token)
        other.cookies.set('acc_csrf', csrf_token)
        assert (await other.get(case['base'] + '/exchange/backup/' + group_backup.json()['snapshot'])).status_code == 200
        assert (await other.get(endpoint + '/' + saved.json()['snapshot'])).status_code == 404
    unpublished = await grant(case, 'fetch', new_sha=private)
    assert (await transfer(case, unpublished.json()['token'])).status_code == 404
    with write_db(case['owner'].app.state.db) as s:
        s.delete(s.get(GroupMembership, (case['group']['id'], 2)))
        s.get(Group, case['group']['id']).revision += 1
        s.commit()
    assert (await case['member'].get(endpoint + '/' + saved.json()['snapshot'])).status_code == 200
    assert (await case['member'].get(case['base'] + '/exchange/backup/' + group_backup.json()['snapshot'])).status_code == 404


async def test_exchange_tokens_expire_and_cannot_be_retargeted(exchange_case, monkeypatch):
    import time
    from types import SimpleNamespace

    from fastapi import HTTPException

    from acc.server.api import group_exchange
    case = exchange_case
    minted = await grant(case)
    wrong = await case['member'].post(case['base'].replace('/projects/' + str(case['group_project']['id']), '/projects/999') + '/exchange/transfer',
        headers={'Authorization': 'Bearer ' + minted.json()['token']}, json={})
    assert wrong.status_code == 404
    assert (await transfer(case, minted.json()['token'])).status_code == 401
    tokens = group_exchange.Tokens()
    token = tokens.mint({'project_id': 1})
    future = time.monotonic() + 61
    monkeypatch.setattr(group_exchange, 'time', SimpleNamespace(monotonic=lambda: future))
    with pytest.raises(HTTPException) as expired:
        tokens.take(token)
    assert expired.value.status_code == 401


async def test_standard_git_http_push_and_viewer_clone_are_scoped(exchange_case, live):
    import asyncio
    import subprocess

    from acc.broker.ops import backup_env
    case = exchange_case
    base = live.replace('ws://', 'http://') + case['base'] + '/exchange/git'
    minted = await grant(case)
    pushed = await asyncio.to_thread(subprocess.run,
        ['git', '-C', str(case['source']), 'push', '--no-verify', base, case['sha'] + ':' + minted.json()['ref']],
        env=backup_env(minted.json()['token'], repo=str(case['source'])), capture_output=True, text=True, timeout=30)
    assert pushed.returncode == 0, pushed.stderr
    assert list((await case['member'].get(case['base'] + '/exchange/refs')).json().values()) == [case['sha']]
    wrong = await grant(case)
    rejected = await asyncio.to_thread(subprocess.run,
        ['git', '-C', str(case['source']), 'push', '--no-verify', base, case['sha'] + ':refs/heads/main'],
        env=backup_env(wrong.json()['token'], repo=str(case['source'])), capture_output=True, text=True, timeout=30)
    assert rejected.returncode != 0
    assert 'refs/heads/main' not in (await case['member'].get(case['base'] + '/exchange/refs')).json()
    from test_gitbackup import commit
    first = case['sha']
    case['sha'] = commit(case['source'], 'README.md', 'next published text')
    forward = await grant(case, old_sha=first)
    advanced = await asyncio.to_thread(subprocess.run,
        ['git', '-C', str(case['source']), 'push', '--no-verify', base, case['sha'] + ':' + forward.json()['ref']],
        env=backup_env(forward.json()['token'], repo=str(case['source'])), capture_output=True, text=True, timeout=30)
    assert advanced.returncode == 0, advanced.stderr
    backwards = await grant(case, old_sha=case['sha'], new_sha=first)
    forced = await asyncio.to_thread(subprocess.run,
        ['git', '-C', str(case['source']), 'push', '--no-verify', '--force', base, first + ':' + backwards.json()['ref']],
        env=backup_env(backwards.json()['token'], repo=str(case['source'])), capture_output=True, text=True, timeout=30)
    assert forced.returncode != 0
    assert list((await case['member'].get(case['base'] + '/exchange/refs')).json().values()) == [case['sha']]
    with write_db(case['owner'].app.state.db) as s:
        s.get(GroupMembership, (case['group']['id'], 2)).role = 'viewer'
        s.get(Group, case['group']['id']).revision += 1
        s.commit()
    fetched = await grant(case, 'fetch')
    destination = case['source'].parent / 'standard-clone'
    cloned = await asyncio.to_thread(subprocess.run, ['git', 'clone', '--no-checkout', base, str(destination)],
        env=backup_env(fetched.json()['token']), capture_output=True, text=True, timeout=30)
    assert cloned.returncode == 0, cloned.stderr
    assert git(destination, 'show', 'HEAD:README.md').stdout == 'next published text'
    assert 'refs/remotes/origin/people/' in git(destination, 'for-each-ref', '--format=%(refname)', 'refs/remotes/origin').stdout
    assert not (destination / 'README.md').exists()
    # Neither a completed Git operation nor its final pack request can be replayed.
    assert (await case['member'].get(case['base'] + '/exchange/git/info/refs', params={'service': 'git-upload-pack'},
        headers={'Authorization': 'Bearer ' + fetched.json()['token']})).status_code == 401
    git(case['source'], 'checkout', '--orphan', 'private')
    private_sha = commit(case['source'], 'secret.txt', 'private-http-canary')
    private_grant = await grant(case, 'fetch')
    refused = await asyncio.to_thread(subprocess.run,
        ['git', '-C', str(destination), 'fetch', base, private_sha], env=backup_env(private_grant.json()['token']),
        capture_output=True, text=True, timeout=30)
    assert refused.returncode != 0
    assert git(destination, 'cat-file', '-e', private_sha, check=False).returncode != 0
    assert (await grant(case)).status_code == 404
    outstanding = await grant(case, 'fetch')
    with write_db(case['owner'].app.state.db) as s:
        s.delete(s.get(GroupMembership, (case['group']['id'], 2)))
        s.get(Group, case['group']['id']).revision += 1
        s.commit()
    assert (await case['member'].get(case['base'] + '/exchange/git/info/refs', params={'service': 'git-upload-pack'},
        headers={'Authorization': 'Bearer ' + outstanding.json()['token']})).status_code == 404


async def test_git_http_upload_has_a_body_deadline(exchange_case, monkeypatch):
    import asyncio

    from acc import git_exchange
    case = exchange_case
    minted = await grant(case)
    monkeypatch.setattr(git_exchange, 'TIMEOUT', 0.02)

    async def slow():
        yield b'0000'
        await asyncio.sleep(0.15)
        yield b'0000'

    response = await case['member'].post(case['base'] + '/exchange/git/git-receive-pack', content=slow(),
        headers={'Authorization': 'Bearer ' + minted.json()['token'], 'Content-Type': 'application/x-git-receive-pack-request'})
    assert response.status_code == 408


async def test_group_diff_reads_only_two_published_commits(exchange_case):
    from acc import git_exchange
    case = exchange_case
    minted = await grant(case)
    pack = base64.b64encode(git_exchange.export(case['source'] / '.git', case['sha'])).decode()
    assert (await transfer(case, minted.json()['token'], pack)).status_code == 200
    response = await case['member'].get(case['base'] + '/published/diff', params={'base': case['sha'], 'commit': case['sha']})
    assert response.status_code == 200, response.text
    assert response.json()['diff'] == ''
    denied = await case['member'].get(case['base'] + '/published/diff', params={'base': 'f' * 40, 'commit': case['sha']})
    assert denied.status_code == 404


@pytest.mark.parametrize('role', ['owner', 'maintainer', 'member', 'viewer', 'nonmember', 'removed'])
async def test_published_diff_role_matrix(exchange_case, role):
    from test_auth import cookie_client

    from acc import git_exchange
    case = exchange_case
    minted = await grant(case)
    pack = base64.b64encode(git_exchange.export(case['source'] / '.git', case['sha'])).decode()
    assert (await transfer(case, minted.json()['token'], pack)).status_code == 200
    actor = cookie_client(case['owner']) if role == 'owner' else case['member']
    with write_db(case['owner'].app.state.db) as s:
        if role == 'owner':
            # Instance administration does not supply a collaboration identity.
            s.add(GroupMembership(group_id=case['group']['id'], user_id=1, role='viewer'))
        else:
            membership = s.get(GroupMembership, (case['group']['id'], 2))
            if role in ('nonmember', 'removed'):
                s.delete(membership)
            else:
                membership.role = role
        s.commit()
    response = await actor.get(case['base'] + '/published/diff', params={'base': case['sha'], 'commit': case['sha']})
    assert response.status_code == (404 if role in ('nonmember', 'removed') else 200), response.text
