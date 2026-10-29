"""Private workspace contracts: local git, project capabilities and explicit promotion."""
# ruff: noqa: F811
import json
import subprocess
from pathlib import Path

from test_auth import live  # noqa: F401
from test_compose_pin import docker  # noqa: F401
from test_gitbackup import served  # noqa: F401
from test_ownership import two  # noqa: F401
from test_users import csrf


async def development(api, **extra):
    response = await api.post('/api/workspaces', json={'name': 'Demo studio', 'kind': 'development', **extra})
    assert response.status_code == 201, response.text
    return response.json()


def git(path, *args):
    return subprocess.check_output(['git', '-C', str(path), *args], text=True).strip()


async def test_development_creation_and_isolated_session(api):
    w = await development(api)
    assert w['private_workspace'] and w['git_backup'] and w['repo'] is None
    path = Path(w['workspace_path'])
    assert git(path, 'branch', '--show-current') == 'main'
    assert git(path, 'remote') == ''
    git(path, 'rev-parse', 'HEAD')
    body = {'project_id': w['id'], 'provider': 'claude', 'name': 'build'}
    preview = await api.post('/api/sessions/preview', json=body)
    assert preview.status_code == 200, preview.text
    assert json.loads(preview.json()['files']['guardrails.json'])['local_only'] is True
    response = await api.post('/api/sessions', json=body)
    assert response.status_code == 201, response.text
    session = response.json()
    assert session['worktree'] and session['worktree_branch'].startswith('acc/build-')
    assert session['cwd'] != str(path)
    assert git(session['cwd'], 'remote') == ''
    assert session['workdir'] == str(path)
    entries = (await api.get('/api/workspaces')).json()
    entry = next(e for e in entries if e['path'] == str(path))
    assert entry['kind'] == 'development' and entry['project']['id'] == w['id']
    assert entry['sessions'][0]['id'] == session['id']
    assert (await api.get(f"/api/projects/{w['id']}/github")).status_code == 409


async def test_owner_conversion_preserves_files_and_promotion(api):
    r = await api.post('/api/sessions', json={'topic': 'Keep notes', 'provider': 'claude', 'name': 'notes', 'profile': 'researcher'})
    assert r.status_code == 201, r.text
    session = r.json()
    await api.post(f"/api/sessions/{session['id']}/stop")
    folder = Path(session['workdir']).name
    converted = await api.post(f'/api/workspaces/{folder}/development')
    assert converted.status_code == 200, converted.text
    w = converted.json()
    assert w['private_workspace'] and Path(w['workspace_path'], 'README.md').exists()
    assert git(w['workspace_path'], 'remote') == ''
    # Converted sessions must not resume their old, permissive launch policy.
    resumed = await api.post(f"/api/sessions/{session['id']}/resume")
    assert resumed.status_code == 409
    promoted = await api.post(f"/api/sessions/{session['id']}/promote", json={'name': 'Keep notes', 'slug': 'keep-notes', 'github': False})
    assert promoted.status_code == 201, promoted.text
    assert promoted.json()['id'] == w['id'] and not promoted.json()['private_workspace']
    assert git(w['workspace_path'], 'remote') == ''


async def test_conversion_refuses_live_sessions_and_existing_remotes(api):
    r = await api.post('/api/sessions', json={'topic': 'Remote notes', 'provider': 'claude', 'name': 'notes', 'profile': 'researcher'})
    session = r.json()
    folder = Path(session['workdir']).name
    assert (await api.post(f'/api/workspaces/{folder}/development')).status_code == 409
    await api.post(f"/api/sessions/{session['id']}/stop")
    git(session['workdir'], 'remote', 'add', 'origin', 'https://example.com/demo.git')
    refused = await api.post(f'/api/workspaces/{folder}/development')
    assert refused.status_code == 409 and 'remotes' in refused.text
    assert git(session['workdir'], 'remote') == 'origin'


async def test_development_available_before_a_session_and_reuse_by_folder(api):
    w = await development(api)
    entry = next(e for e in (await api.get('/api/workspaces')).json() if e['path'] == w['workspace_path'])
    assert entry['sessions'] == []
    response = await api.post('/api/sessions', json={'workspace': entry['folder'], 'provider': 'claude', 'name': 'build', 'profile': 'frontend'})
    assert response.status_code == 201, response.text
    assert response.json()['project_id'] == w['id'] and response.json()['worktree']



async def test_member_private_workspace_is_their_account_and_others_cannot_use_it(api, two, env):
    member = two['member']
    response = await member.post('/api/workspaces', json={'name': 'Member studio'}, headers=csrf(member))
    assert response.status_code == 201, response.text
    w = response.json()
    assert w['owner'] == 'alex' and w['account'] == 'dana-agent'
    assert w['workspace_path'].startswith(str(env['settings'].account_root('dana-agent')) + '/local/')
    body = {'project_id': w['id'], 'provider': 'claude', 'name': 'private build'}
    session = await member.post('/api/sessions', json=body, headers=csrf(member))
    assert session.status_code == 201 and session.json()['run_as'] == 'dana-agent'
    assert (await member.post(f"/api/workspaces/{Path(w['workspace_path']).name}/development", headers=csrf(member))).status_code == 403
    # The member cannot launch or read the owner's private environment.
    other = await development(api, name='Owner studio')
    assert other['id'] not in {p['id'] for p in (await member.get('/api/projects')).json()}
    assert other['workspace_path'] not in {p['path'] for p in (await member.get('/api/workspaces')).json()}
    for url in [f"/api/files/project/{other['id']}", f"/api/projects/{other['id']}/runtime", f"/api/projects/{other['id']}/backup", f"/api/projects/{other['id']}/keys"]:
        assert (await member.get(url)).status_code == 404
    assert (await member.post('/api/sessions', json={**body, 'project_id': other['id']}, headers=csrf(member))).status_code == 404


async def test_development_capabilities_files_changes_keys_ports_compose_budget_backup(api, env, docker, served):
    from helpers import wait_for
    from test_budgets import seed
    w = await development(api)
    pid, root = w['id'], Path(w['workspace_path'])
    port = await api.post(f'/api/projects/{pid}/ports', json={'range': 'dev'})
    assert port.status_code == 201 and 8000 <= port.json()['port'] <= 8099
    key = await api.post('/api/vault/keys', json={'name': 'Demo key', 'variable': 'DEMO_KEY', 'value': 'demo-value-123456789'})
    assert key.status_code == 201, key.text
    assert (await api.post(f"/api/vault/keys/{key.json()['id']}/give", json={'project_id': pid})).status_code == 200
    body = {'project_id': pid, 'provider': 'claude', 'name': 'capabilities'}
    preview = (await api.post('/api/sessions/preview', json=body)).json()
    assert preview['keys']['keys'][0]['variable'] == 'DEMO_KEY'
    (root / 'compose.yaml').write_text('services:\n  web:\n    image: nginx\n')
    assert (await api.post(f'/api/projects/{pid}/runtime', json={'compose_autostart': True})).status_code == 409
    review = (await api.get(f'/api/projects/{pid}/runtime')).json()['compose']
    assert not review['approved']
    approved = await api.post(f'/api/projects/{pid}/runtime', json={'compose_autostart': True, 'digest': review['digest']})
    assert approved.status_code == 200 and approved.json()['compose_approved']
    (root / 'compose.yaml').write_text('services:\n  web:\n    image: nginx:2\n')
    assert not (await api.get(f'/api/projects/{pid}/runtime')).json()['compose']['approved']
    session = (await api.post('/api/sessions', json=body)).json()
    async def launched():
        events = (await api.get(f"/api/sessions/{session['id']}/events")).json()
        return any(e['kind'] == 'run_launch' for e in events)
    assert await wait_for(launched)
    assert 'DEMO_KEY=demo-value-123456789' in (env['run_dir'] / session['id'] / 'session.env').read_text()
    (Path(session['cwd']) / 'notes.md').write_text('# Demo preview\n')
    changes = await api.get(f"/api/sessions/{session['id']}/changes")
    assert changes.status_code == 200 and 'notes.md' in changes.text
    files = await api.get(f"/api/files/session/{session['id']}/read", params={'path': 'notes.md'})
    assert files.status_code == 200 and '# Demo preview' in files.json()['content']
    backup = await api.post(f'/api/projects/{pid}/backup/run')
    assert backup.status_code == 200 and backup.json()['last_at'], backup.text
    assert {b['name'] for b in backup.json()['repo']['branches']} >= {'main', session['worktree_branch']}
    assert git(root, 'remote') == ''
    seed(api, used=85)
    await api.put('/api/budgets', json={'rules': {'claude.seven_day': {'mode': 'cap', 'limit': 80, 'action': 'pause'}}})
    refused = await api.post('/api/sessions', json={**body, 'name': 'over budget'})
    assert refused.status_code == 409 and 'budget pauses' in refused.text


async def test_development_github_promotion_preserves_capabilities_and_backup_identity(api, env, served, monkeypatch, tmp_path):
    from test_local_workspace import FakeGitHub
    for key, value in {'GIT_AUTHOR_NAME': 'Alex Example', 'GIT_AUTHOR_EMAIL': 'alex@example.com', 'GIT_COMMITTER_NAME': 'Alex Example', 'GIT_COMMITTER_EMAIL': 'alex@example.com'}.items():
        monkeypatch.setenv(key, value)
    fake = FakeGitHub()
    monkeypatch.setattr(type(api.app.state.supervisor), 'github', property(lambda self: fake))
    w = await development(api)
    pid = w['id']
    session = (await api.post('/api/sessions', json={'project_id': pid, 'name': 'promote', 'provider': 'claude'})).json()
    await api.post(f'/api/projects/{pid}/ports', json={'port': 8005})
    before = (await api.post(f'/api/projects/{pid}/backup/run')).json()
    assert before['last_at']
    remote = tmp_path / 'remotes' / 'casey' / 'demo-studio.git'
    subprocess.run(['git', 'init', '-q', '--bare', '-b', 'main', str(remote)], check=True)
    url = f"/api/sessions/{session['id']}/promote"
    assert (await api.post(url, json={'name': 'Demo studio', 'slug': 'demo-studio'})).status_code == 409
    promoted = await api.post(url, json={'name': 'Demo studio', 'slug': 'demo-studio', 'confirm': True})
    assert promoted.status_code == 201, promoted.text
    assert promoted.json()['id'] == pid and promoted.json()['repo'] == 'casey/demo-studio'
    assert promoted.json()['slug'] == 'demo-studio' and not promoted.json()['private_workspace']
    assert (await api.get(f'/api/projects/{pid}/backup')).json()['repo']['branches']
    assert next(p for p in (await api.get('/api/ports')).json() if p['port'] == 8005)['project_id'] == pid
    assert git(w['workspace_path'], 'remote') == 'origin'
    preview = (await api.post('/api/sessions/preview', json={'project_id': pid, 'name': 'project', 'provider': 'claude'})).json()
    assert not json.loads(preview['files']['guardrails.json'])['local_only']


async def test_conversion_does_not_mix_identical_folder_names_across_accounts(api, two, env):
    env['settings'].broker_env_dir.mkdir(exist_ok=True)
    (env['settings'].broker_env_dir / 'dana-agent.env').write_text(f"ACC_ALLOWED_ROOTS={env['workspaces'] / 'member'}\n")
    member = two['member']
    body = {'topic': 'Same topic', 'name': 'notes', 'provider': 'claude', 'profile': 'researcher'}
    mine = (await api.post('/api/sessions', json=body)).json()
    theirs = (await member.post('/api/sessions', json=body, headers=csrf(member))).json()
    assert Path(mine['workdir']).name == Path(theirs['workdir']).name
    await api.post(f"/api/sessions/{mine['id']}/stop")
    # Their live session should neither block my conversion nor get reassigned to my workspace.
    converted = await api.post(f"/api/workspaces/{Path(mine['workdir']).name}/development")
    assert converted.status_code == 200, converted.text
    after = (await member.get(f"/api/sessions/{theirs['id']}")).json()
    assert after['project_id'] == theirs['project_id']
