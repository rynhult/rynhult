"""Bounded Git action regressions: real local commits and recorded transport argv."""
import subprocess

import pytest
from helpers import verified_post, write_db
from isolated_settings import isolated_broker
from test_auth import do_setup
from test_changes import git, repo
from test_pulls import fake, world  # noqa: F401, F811
from test_users import browser, csrf, join

from acc.broker import ops, session_git
from acc.server.models import AuditLog, Session


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / 'repo'
    repo(root)
    git(root, 'config', 'user.name', 'Agent')
    git(root, 'config', 'user.email', 'agent@example.com')
    git(root, 'checkout', '-qb', 'feature')
    config = isolated_broker(tmp_path, allowed_roots=(tmp_path,), run_dir=tmp_path / 'run')
    return config, root


def test_commit_message_identity_and_ignored_files(checkout):
    config, root = checkout
    (root / '.gitignore').write_text('ignored\n')
    (root / 'ignored').write_text('hidden')
    (root / 'new').write_text('new')
    (root / 'app.py').unlink()
    message = 'Fix "quotes" and $(touch bad)\n\nSecond line with `ticks`'
    result = session_git.operate(config, str(root), 'commit', person='person', message=message, fingerprint=session_git.diff_fingerprint(root))
    assert result['commit'] == git(root, 'rev-parse', 'HEAD')
    assert git(root, 'log', '-1', '--format=%B') == message + '\n\nRynhult-Person: person'
    assert git(root, 'log', '-1', '--format=%an') == 'Agent'
    assert git(root, 'ls-tree', '--name-only', 'HEAD').splitlines() == ['.gitignore', 'new']
    assert not (root / 'bad').exists()


@pytest.mark.parametrize('action', ['commit', 'push'])
def test_busy_and_detached(checkout, action):
    config, root = checkout
    lock = root / '.git/index.lock'
    lock.touch()
    with pytest.raises(ops.OpError, match='agent is busy, try again'):
        session_git.operate(config, str(root), action, message='message', person='person')
    lock.unlink()
    git(root, 'checkout', '--detach')
    with pytest.raises(ops.OpError):
        session_git.operate(config, str(root), action, message='message', person='person')


def test_push_refusals_and_no_force(checkout, monkeypatch):
    config, root = checkout
    git(root, 'remote', 'add', 'origin', 'git@github.com:example/shop.git')
    with pytest.raises(ops.OpError, match='protected'):
        session_git.operate(config, str(root), 'push', branch='feature', default_branch='feature')
    original = subprocess.run
    calls = []

    def run(argv, **kwargs):
        if 'push' in argv:
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 1, 'remote refused', '')
        return original(argv, **kwargs)

    monkeypatch.setattr(subprocess, 'run', run)
    result = session_git.operate(config, str(root), 'push', branch='feature', default_branch='main', repo='example/shop')
    assert not result['ok'] and result['output'] == 'remote refused'
    assert calls[0][-3:] == ['--porcelain', 'git@github.com:example/shop.git', 'HEAD:refs/heads/feature']
    assert not any(a.startswith(('--force', '+')) or a == '-f' for a in calls[0])
    with pytest.raises(ops.OpError, match='guardrail'):
        session_git.operate(config, str(root), 'push', custom=[{'pattern': 'git push'}])


async def test_api_commit_permissions_and_audit(api, env, world, member_accounts):  # noqa: F811
    sid = world['session']['id']
    url = f'/api/sessions/{sid}/git/commit'
    # Existing sessions in the shared checkout cannot bypass the default guardrail.
    assert (await api.post(url, json={'message': 'change'})).status_code == 403
    with write_db(api.app.state.db) as db:
        db.get(Session, sid).worktree = str(world['path'])
        db.commit()
    git(world['path'], 'config', 'user.name', 'Agent')
    git(world['path'], 'config', 'user.email', 'agent@example.com')
    (world['path'] / 'new.txt').write_text('new')
    response = await api.post(url, json={'message': 'Person commit\n\n"quoted"', 'fingerprint': (await api.get(f'/api/sessions/{sid}/changes')).json()['fingerprint']})
    assert response.status_code == 200, response.text
    assert response.json()['commit']
    assert (await api.post(url, json={'message': '   '})).status_code >= 400
    with write_db(api.app.state.db) as db:
        from sqlalchemy import select
        rows = list(db.scalars(select(AuditLog).where(AuditLog.target == sid, AuditLog.action == 'git.commit')))
        assert any(row.detail['outcome'] == 'success' for row in rows)
        assert any(row.detail['outcome'] == 'refused' for row in rows)
    await do_setup(api, env)
    async with browser(api) as other:
        link = (await verified_post(api, '/api/users/invites', json={'role': 'member', 'account': 'dana-agent'})).json()['link']
        await join(other, link, 'other')
        assert (await other.post(url, headers=csrf(other), json={'message': 'forbidden'})).status_code == 404
        assert (await other.post('/api/sessions/nonexistent/git/commit', headers=csrf(other), json={'message': 'forbidden'})).status_code == 404
    with write_db(api.app.state.db) as db:
        from sqlalchemy import select
        rows = list(db.scalars(select(AuditLog).where(AuditLog.actor == 'other', AuditLog.action == 'git.commit')))
        assert {row.target for row in rows} == {sid, 'nonexistent'}
        assert all(row.detail['reason'] == "That session wasn't found." for row in rows)


@pytest.mark.parametrize('protection', ['default', 'protected', 'rules', 'unreadable', 'allowed'])
async def test_api_push_branch_protection(api, world, fake, monkeypatch, protection):  # noqa: F811
    from acc.server import pulls
    sid = world['session']['id']
    with write_db(api.app.state.db) as db:
        db.get(Session, sid).worktree = str(world['path'])
        db.commit()
    branch = 'feature/login'
    if protection == 'default':
        fake.info['default_branch'] = branch
    if protection == 'protected':
        fake.protection[branch] = {'required_status_checks': {'contexts': ['test']}}
    if protection == 'rules':
        fake.rules[branch] = [{'type': 'pull_request'}]
    github = api.app.state.supervisor.github
    if protection == 'unreadable':
        original_request = github.request

        async def request(method, path, *args, **kwargs):
            if '/rules/branches/' in path:
                return 403, {}
            return await original_request(method, path, *args, **kwargs)

        monkeypatch.setattr(github, 'request', request)
    monkeypatch.setattr(pulls, 'BrokerGitHub', lambda *args: github)
    broker = api.app.state.supervisor.broker
    original_call = broker.call
    pushes = []

    async def call(method, **params):
        if method == 'session_git' and params['action'] == 'push':
            pushes.append(params)
            return {'ok': True, 'output': 'pushed'}
        return await original_call(method, **params)

    monkeypatch.setattr(broker, 'call', call)
    response = await api.post(f'/api/sessions/{sid}/git/push', json={})
    assert response.status_code == (200 if protection == 'allowed' else 403), response.text
    assert bool(pushes) == (protection == 'allowed')
    with write_db(api.app.state.db) as db:
        from sqlalchemy import select
        row = db.scalar(select(AuditLog).where(AuditLog.target == sid, AuditLog.action == 'git.push'))
        assert row.detail['outcome'] == ('success' if protection == 'allowed' else 'refused')


def test_reviewed_changes_and_trailer(checkout):
    config, root = checkout
    target = root / 'new.bin'
    target.write_bytes(b'\0one')
    reviewed = ops.workspace_changes(config, str(root), None, detail=True)['fingerprint']
    target.write_bytes(b'\0two')
    with pytest.raises(ops.OpError, match='changes moved, review again'):
        session_git.operate(config, str(root), 'commit', person='person', message='message', fingerprint=reviewed)
    assert not (root / '.git/index.lock').exists()
    reviewed = session_git.diff_fingerprint(root)
    result = session_git.operate(config, str(root), 'commit', person='person',
        message='message\nRynhult-Person: forged\n  rynhult-person: another', fingerprint=reviewed)
    assert result['file_count'] == 1
    assert git(root, 'log', '-1', '--format=%B') == 'message\n\nRynhult-Person: person'


def test_push_ignores_hostile_config(checkout, monkeypatch):
    config, root = checkout
    git(root, 'remote', 'add', 'origin', 'https://github.com/example/shop.git')
    account_home = root.parent / 'account-home'
    account_home.mkdir()
    (account_home / '.gitconfig').write_text('[credential]\n helper = store\n')
    monkeypatch.setenv('HOME', str(account_home))
    marker = root / 'executed'
    for key, value in [('core.sshCommand', f'touch {marker}'), ('credential.helper', f'!touch {marker}'),
                       ('http.proxy', 'http://example.invalid'), ('http.sslVerify', 'false'),
                       ('push.gpgSign', 'true'), ('gpg.program', f'touch {marker}'),
                       ('push.recurseSubmodules', 'on-demand')]:
        git(root, 'config', key, value)
    original = subprocess.run
    observed = []

    def run(argv, **kwargs):
        if 'push' in argv:
            # Query effective transport configuration with real Git, without network.
            prefix = argv[:argv.index('push')]
            for key, expected in [('http.sslVerify', 'true'), ('http.proxy', ''), ('core.sshCommand', 'ssh'),
                                  ('push.gpgSign', 'false'), ('push.recurseSubmodules', 'no')]:
                actual = original([*prefix, 'config', '--get', key], env=kwargs['env'], capture_output=True, text=True)
                assert actual.stdout.strip() == expected
            helpers = original([*prefix, 'config', '--get-all', 'credential.helper'],
                env=kwargs['env'], capture_output=True, text=True)
            assert helpers.stdout.splitlines() == ['', 'store']
            assert original([*prefix, 'cat-file', '-t', 'HEAD'], env=kwargs['env'],
                capture_output=True, text=True).stdout.strip() == 'commit'
            assert kwargs['env']['GIT_CONFIG_NOSYSTEM'] == '1'
            assert kwargs['env']['GIT_CONFIG_GLOBAL'] == '/dev/null'
            assert argv[-2] == 'https://github.com/example/shop.git'
            git(root, 'remote', 'set-url', 'origin', 'https://github.com/evil/changed.git')
            observed.append(argv)
            return subprocess.CompletedProcess(argv, 0, 'pushed', '')
        return original(argv, **kwargs)

    monkeypatch.setattr(subprocess, 'run', run)
    session_git.operate(config, str(root), 'push', branch='feature', default_branch='main', repo='example/shop')
    assert observed and not marker.exists()


@pytest.mark.parametrize('revision', [None, 'stale'])
def test_commit_requires_review(checkout, revision):
    config, root = checkout
    (root / 'new').write_text('new')
    before = git(root, 'rev-parse', 'HEAD')
    with pytest.raises(ops.OpError, match='changes moved, review again'):
        session_git.operate(config, str(root), 'commit', person='person', message='message', fingerprint=revision)
    assert git(root, 'rev-parse', 'HEAD') == before
    assert git(root, 'diff', '--cached') == ''


def test_changes_during_staging_refused(checkout, monkeypatch):
    config, root = checkout
    target = root / 'new'
    target.write_text('reviewed')
    reviewed = session_git.diff_fingerprint(root)
    original = subprocess.run

    def run(argv, **kwargs):
        result = original(argv, **kwargs)
        if 'add' in argv:
            assert (root / '.git/index.lock').exists()
            target.write_text('agent changed it')
        return result

    monkeypatch.setattr(subprocess, 'run', run)
    with pytest.raises(ops.OpError, match='changes moved, review again'):
        session_git.operate(config, str(root), 'commit', person='person', message='message', fingerprint=reviewed)
    assert git(root, 'diff', '--cached') == ''
    assert not (root / '.git/index.lock').exists()


@pytest.mark.parametrize('action', ['commit', 'push'])
async def test_api_git_errors_redacted(api, world, monkeypatch, action):  # noqa: F811
    from acc.server.broker_client import BrokerError
    from acc.server.vault import Redactor
    sid = world['session']['id']
    with write_db(api.app.state.db) as db:
        db.get(Session, sid).worktree = str(world['path'])
        db.commit()
    canary = 'test-vault-canary-6e7d5c4b'
    supervisor = api.app.state.supervisor
    monkeypatch.setattr(supervisor.vault, 'redactor', lambda *args: Redactor([(canary, 'Test key')]))

    async def fail(method, **params):
        raise BrokerError('Git error: ' + canary)

    monkeypatch.setattr(supervisor.broker, 'call', fail)
    response = await api.post(f'/api/sessions/{sid}/git/{action}', json={'message': 'change'})
    assert response.status_code >= 400
    assert canary not in response.text
    assert 'Test key' in response.text
    with write_db(api.app.state.db) as db:
        from sqlalchemy import select
        rows = list(db.scalars(select(AuditLog).where(AuditLog.target == sid, AuditLog.action == 'git.' + action)))
        assert rows and all(canary not in str(row.detail) for row in rows)
        assert any('Test key' in row.detail.get('reason', '') for row in rows)


async def test_api_commit_stale_review(api, world):  # noqa: F811
    sid = world['session']['id']
    root = world['path']
    with write_db(api.app.state.db) as db:
        db.get(Session, sid).worktree = str(root)
        db.commit()
    (root / 'new').write_text('reviewed')
    review = (await api.get(f'/api/sessions/{sid}/changes')).json()['fingerprint']
    before = git(root, 'rev-parse', 'HEAD')
    (root / 'new').write_text('changed')
    response = await api.post(f'/api/sessions/{sid}/git/commit', json={'message': 'change', 'fingerprint': review})
    assert response.status_code >= 400
    assert 'changes moved, review again' in response.text
    assert git(root, 'rev-parse', 'HEAD') == before


@pytest.mark.parametrize('action', ['commit', 'push'])
def test_container_git_mutations_refused(checkout, monkeypatch, action):
    config, root = checkout
    monkeypatch.setattr(ops.containers, 'is_container_checkout', lambda cwd: True)
    with pytest.raises(ops.OpError, match='read-only'):
        session_git.operate(config, str(root), action, person='person', message='message')


def test_subfolder_review_cannot_commit_unseen_files(checkout):
    config, root = checkout
    folder = root / 'folder'
    folder.mkdir()
    (folder / 'visible').write_text('visible')
    (root / 'unseen').write_text('unseen')
    with pytest.raises(ops.OpError, match='repository root'):
        session_git.operate(config, str(folder), 'commit', person='person', message='message',
                            fingerprint=session_git.diff_fingerprint(folder))


@pytest.mark.parametrize('action', ['commit', 'push'])
async def test_api_git_offline_status(api, world, monkeypatch, action):  # noqa: F811
    from acc.server.broker_client import MachineOffline
    sid = world['session']['id']
    with write_db(api.app.state.db) as db:
        db.get(Session, sid).worktree = str(world['path'])
        db.commit()
    async def offline(*args, **kwargs):
        raise MachineOffline('Machine offline: example.')
    monkeypatch.setattr(api.app.state.supervisor.broker, 'call', offline)
    response = await api.post(f'/api/sessions/{sid}/git/{action}', json={'message': 'change'})
    assert response.status_code == 409
    assert 'Machine offline' in response.text


def test_push_refuses_ambiguous_credential_key(checkout, monkeypatch):
    config, root = checkout
    git(root, 'remote', 'add', 'origin', 'https://github.com/example/shop.git')
    home = root.parent / 'credential-home'
    home.mkdir()
    (home / '.gitconfig').write_text('[credential "https://example.com/?x=y"]\n helper = store\n')
    monkeypatch.setenv('HOME', str(home))
    with pytest.raises(ops.OpError, match='keys containing ='):
        session_git.operate(config, str(root), 'push', branch='feature', default_branch='main', repo='example/shop')


@pytest.mark.parametrize('container,truncated', [(True, False), (False, True), (False, False)])
async def test_changes_action_visibility(api, world, monkeypatch, container, truncated):  # noqa: F811
    sid = world['session']['id']
    with write_db(api.app.state.db) as db:
        session = db.get(Session, sid)
        session.worktree = str(world['path'])
        session.container = container
        db.commit()
    broker = api.app.state.supervisor.broker
    original = broker.call
    async def call(method, **params):
        if method == 'workspace_changes':
            return {'git': True, 'files': 1, 'truncated': truncated}
        return await original(method, **params)
    monkeypatch.setattr(broker, 'call', call)
    response = await api.get(f'/api/sessions/{sid}/changes')
    assert response.status_code == 200, response.text
    assert response.json()['git_actions_allowed'] is (not container and not truncated)
