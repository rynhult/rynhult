"""Registration and trust refuse policy boundaries with a repair path, before side effects."""
import pytest

from acc.server.models import Project


async def test_registration_outside_roots_is_actionable(api, env):
    path = env['home'] / 'external'
    path.mkdir()
    response = await api.post('/api/projects', json={
        'slug': 'external', 'name': 'External', 'workspace_path': str(path)})
    assert response.status_code == 409, response.text
    assert 'Security → Allowed folders' in response.json()['detail']
    assert not (await api.get('/api/projects')).json()


@pytest.mark.parametrize('message', [
    'Allow this folder in Security → Allowed folders.',
    "/home/example/repo is outside this account's workspace folders. Choose a folder inside them.",
])
async def test_legacy_trust_refusal_is_actionable(api, env, monkeypatch, message):
    from acc.server.broker_client import BrokerError
    with api.app.state.db() as db:
        project = Project(slug='external', name='External', workspace_path=str(env['home'] / 'external'),
                          account=env['settings'].default_account)
        db.add(project)
        db.commit()
        project_id = project.id
    original = api.app.state.ctx.broker.call
    async def call(method, **params):
        if method == 'trust_workspace':
            raise BrokerError(message)
        return await original(method, **params)
    monkeypatch.setattr(api.app.state.ctx.broker, 'call', call)
    response = await api.post(f'/api/projects/{project_id}/trust')
    assert response.status_code == 409, response.text
    assert 'Security → Allowed folders' in response.json()['detail']
    with api.app.state.db() as db:
        assert not db.get(Project, project_id).trusted


async def test_legacy_outside_trust_uses_real_broker_refusal(api, env):
    path = env['home'] / 'outside-legacy'
    path.mkdir()
    with api.app.state.db() as db:
        project = Project(slug='legacy', name='Legacy', workspace_path=str(path),
                          account=env['settings'].default_account)
        db.add(project)
        db.commit()
        project_id = project.id
    response = await api.post(f'/api/projects/{project_id}/trust')
    assert response.status_code == 409, response.text
    assert 'Allow this folder in Security → Allowed folders' in response.json()['detail']
    with api.app.state.db() as db:
        assert not db.get(Project, project_id).trusted
    assert list(path.iterdir()) == []


@pytest.mark.parametrize('message', ['node is offline', 'Unknown method: workspace_identity'])
async def test_remote_registration_requires_online_updated_node(api, env, monkeypatch, message):
    from acc.server.broker_client import BrokerError
    from acc.server.models import Node
    account = env['settings'].default_account
    with api.app.state.db() as db:
        db.add(Node(id='remote-register', name='example-node', status='active', info={'accounts': [account]}))
        db.commit()
    original = api.app.state.ctx.broker.call
    async def call(method, **params):
        if method == 'workspace_identity' and params.get('host') == 'example-node':
            raise BrokerError(message)
        return await original(method, **params)
    monkeypatch.setattr(api.app.state.ctx.broker, 'call', call)
    response = await api.post('/api/projects', json={'slug': 'remote', 'name': 'Remote',
        'workspace_path': '/home/example/repo', 'host': 'example-node', 'account': account})
    assert response.status_code == 409, response.text
    assert 'node must be online and updated' in response.json()['detail']
    assert not (await api.get('/api/projects')).json()
