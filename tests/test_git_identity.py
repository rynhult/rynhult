"""Project-local Git identity, through the real broker and isolated Git repositories."""
import os
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import select
from test_auth import cookie_client, do_setup
from test_changes import repo
from test_orchestrator_assignments import runtime  # noqa: F401
from test_team import grant
from test_team import request as team_request

from acc.server.models import AuditLog


def plain_git(path, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null')
    return subprocess.run(['git', '-C', str(path), *args], env=env, capture_output=True, text=True, check=True).stdout.strip()


async def person(api, env):
    await do_setup(api, env)
    cookie_client(api)
    api.headers.update({'Origin': 'http://acc', 'X-ACC-CSRF': api.cookies.get('acc_csrf')})


async def test_empty_project_seeds_owner_identity_and_plain_worktree_commit(api, env):
    await person(api, env)
    response = await api.post('/api/projects/new', json={'name': 'Fresh owner project', 'source': 'empty'})
    assert response.status_code == 201, response.text
    project = response.json()
    root = Path(project['workspace_path'])
    assert plain_git(root, 'log', '-1', '--format=%s') == 'Initialize project'
    assert plain_git(root, 'log', '-1', '--format=%an') == 'Rynhult'
    identity = await api.get(f"/api/projects/{project['id']}/git-identity")
    assert identity.status_code == 200, identity.text
    assert identity.json()['name'] == 'casey'
    assert identity.json()['email'] == 'casey@noreply.rynhult.invalid'
    assert plain_git(root, 'config', '--local', 'user.name') == 'casey'
    response = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude',
                                                   'name': 'Commit from own checkout', 'worktree': True})
    assert response.status_code == 201, response.text
    tree = Path(response.json()['worktree'])
    (tree / 'change.txt').write_text('The helper can commit without -c identity flags.\n')
    plain_git(tree, 'add', 'change.txt')
    plain_git(tree, 'commit', '-qm', 'Plain helper commit')
    assert plain_git(tree, 'log', '-1', '--format=%an <%ae>') == 'casey <casey@noreply.rynhult.invalid>'
    assert not (env['home'] / '.gitconfig').exists()
    with api.app.state.db() as db:
        rows = list(db.scalars(select(AuditLog).where(AuditLog.action == 'project.git_identity')))
        assert len(rows) == 1 and rows[0].actor == 'casey'
        assert rows[0].detail['name'] == 'casey'


async def test_launch_repairs_missing_identity_but_preserves_existing_and_owner_edits(api, env, project):
    root = Path(project['workspace_path'])
    repo(root)  # initial commit uses command-local flags, never saved config
    response = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude', 'name': 'First'})
    assert response.status_code == 201, response.text
    assert plain_git(root, 'config', '--local', 'user.name') == 'Rynhult'
    plain_git(root, 'config', '--local', 'user.name', 'Repository author')
    plain_git(root, 'config', '--local', 'user.email', 'author@example.com')
    response = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude', 'name': 'Again'})
    assert response.status_code == 201, response.text
    assert plain_git(root, 'config', '--local', 'user.name') == 'Repository author'
    assert plain_git(root, 'config', '--local', 'user.email') == 'author@example.com'
    changed = await api.put(f"/api/projects/{project['id']}/git-identity", json={'name': 'Project author', 'email': 'project@example.com'})
    assert changed.status_code == 200, changed.text
    assert plain_git(root, 'config', '--local', 'user.name') == 'Project author'


@pytest.mark.parametrize('body', [{'name': 'Unsafe\nName', 'email': 'a@example.com'},
                                  {'name': 'Valid', 'email': 'not an email'},
                                  {'name': '', 'email': 'a@example.com'}])
async def test_invalid_project_identity_is_rejected(api, project, body):
    response = await api.put(f"/api/projects/{project['id']}/git-identity", json=body)
    assert response.status_code == 422


async def test_fresh_project_team_helper_commits_with_inherited_identity(runtime, api, env):  # noqa: F811
    await person(api, env)
    response = await api.post('/api/projects/new', json={'name': 'Fresh team', 'source': 'empty'})
    assert response.status_code == 201, response.text
    project = response.json()
    lead = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude',
                                               'name': 'Lead', 'worktree': True, 'orchestrator': grant()})
    assert lead.status_code == 201, lead.text
    lead = lead.json()
    assignment = await team_request(api, env, lead, 'assign', goal='Save a small project change',
                                    task_type='implementation', acceptance=['Commit succeeds'], requirement_ids=[],
                                    need={'role': 'general', 'classification_reason': 'Small project change'})
    assert assignment['ok'], assignment
    choice = await team_request(api, env, lead, 'select', assignment=assignment['result']['id'])
    assert choice['ok'], choice
    started = await team_request(api, env, lead, 'start', assignment=assignment['result']['id'],
                                selection=choice['result']['id'], name='Committer')
    assert started['ok'], started
    helper = started['result']
    root = Path(helper['worktree'])
    (root / 'helper.txt').write_text('A real helper worktree commit.\n')
    plain_git(root, 'add', 'helper.txt')
    plain_git(root, 'commit', '-qm', 'Helper change')
    assert plain_git(root, 'log', '-1', '--format=%an <%ae>') == 'casey <casey@noreply.rynhult.invalid>'
    text = (Path(env['run_dir']) / lead['id'] / 'append-prompt.md').read_text()
    assert f"<date>-{lead['id'][:8]}-<slug>.md" in text
    helper_text = (Path(env['run_dir']) / helper['id'] / 'append-prompt.md').read_text()
    assert '<attempt8>' in helper_text and 'Use your actual attempt ID' in helper_text


def test_effective_global_identity_is_not_replaced(env):
    from acc.broker import ops
    root = env['workspaces'] / 'with-global'
    repo(root)
    global_config = env['home'] / '.gitconfig'
    global_config.write_text('[user]\nname = Existing global author\nemail = global@example.com\n')
    result = ops.git_identity(env['broker'], str(root), 'Fallback', 'fallback@example.com')
    assert result['changed'] == []
    assert result['name'] == 'Existing global author' and result['email'] == 'global@example.com'
    assert '[user]' not in (root / '.git/config').read_text()


def test_repository_config_link_cannot_change_account_config(env):
    from acc.broker import ops
    root = env['workspaces'] / 'linked-config'
    repo(root)
    account_config = env['home'] / 'identity-target'
    account_config.write_bytes((root / '.git/config').read_bytes())
    before = account_config.read_bytes()
    (root / '.git/config').unlink()
    (root / '.git/config').symlink_to(account_config)
    with pytest.raises(ops.OpError, match='regular file'):
        ops.git_identity(env['broker'], str(root), 'Changed', 'changed@example.com', replace=True)
    assert account_config.read_bytes() == before


async def test_register_folder_defers_identity_setup_to_broker_launch(api, env, monkeypatch):
    root = env['workspaces'] / 'registered-repository'
    repo(root)
    broker = api.app.state.supervisor.broker
    original = broker.call
    identity_calls = []

    async def call(method, **params):
        if method == 'git_identity':
            identity_calls.append(params)
        return await original(method, **params)

    monkeypatch.setattr(broker, 'call', call)
    response = await api.post('/api/projects', json={'slug': 'registered-repository', 'name': 'Registered repository',
                                                   'workspace_path': str(root)})
    assert response.status_code == 201, response.text
    assert identity_calls == []
    assert '[user]' not in (root / '.git/config').read_text()
    project = response.json()
    trusted = await api.post(f"/api/projects/{project['id']}/trust")
    assert trusted.status_code == 200, trusted.text
    launched = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude',
                                                   'name': 'Use registered repository'})
    assert launched.status_code == 201, launched.text
    assert identity_calls and all(call['path'] == str(root) for call in identity_calls)
    assert plain_git(root, 'config', '--local', 'user.name') == 'Rynhult'


@pytest.mark.parametrize('existing', [False, True])
async def test_identity_broker_failure_never_becomes_unhandled_launch_error(api, project, env, monkeypatch, existing):
    from acc.server.broker_client import BrokerError
    from acc.server.models import Event
    root = Path(project['workspace_path'])
    repo(root)
    if existing:
        plain_git(root, 'config', 'user.name', 'Existing Author')
        plain_git(root, 'config', 'user.email', 'existing@example.com')
    broker = api.app.state.supervisor.broker
    original = broker.call
    async def call(method, **params):
        if method == 'git_identity':
            raise BrokerError('OpError: PRIVATE raw git failure')
        return await original(method, **params)
    monkeypatch.setattr(broker, 'call', call)
    response = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude', 'name': 'Identity warning'})
    assert response.status_code == 201, response.text
    with api.app.state.db() as db:
        event = db.scalar(select(Event).where(Event.session_id == response.json()['id'], Event.kind == 'git_identity_warning'))
        assert event and 'project page' in event.payload['message']
        assert 'PRIVATE' not in str(event.payload) and 'OpError' not in str(event.payload)


async def test_new_project_identity_failure_returns_saved_project_with_actionable_warning(api, env, monkeypatch):
    from acc.server.broker_client import BrokerError
    broker = api.app.state.supervisor.broker
    original = broker.call
    async def call(method, **params):
        if method == 'git_identity':
            raise BrokerError('OpError: PRIVATE raw git failure')
        return await original(method, **params)
    monkeypatch.setattr(broker, 'call', call)
    response = await api.post('/api/projects/new', json={'name': 'Identity unavailable', 'source': 'empty'})
    assert response.status_code == 201, response.text
    assert 'project page' in response.json()['git_identity_warning']
    assert 'PRIVATE' not in response.text and 'OpError' not in response.text
    assert response.json()['id'] in [p['id'] for p in (await api.get('/api/projects')).json()]
    with api.app.state.db() as db:
        warning = db.scalar(select(AuditLog).where(AuditLog.action == 'project.git_identity_failed'))
        assert warning and 'project page' in warning.detail['message']
