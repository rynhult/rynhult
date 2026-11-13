"""Folder registration identities are owned by the selected agent account."""
import asyncio

from acc.server.models import Project


async def test_duplicate_folder_and_symlink_offer_existing_project(api, project, env):
    path = env['workspaces'] / 'demo'
    alias = env['workspaces'] / 'demo-alias'
    alias.symlink_to(path, target_is_directory=True)
    for folder in [path, alias]:
        response = await api.post('/api/projects', json={
            'slug': 'duplicate', 'name': 'Duplicate', 'workspace_path': str(folder)})
        assert response.status_code == 409, response.text
        assert response.json()['detail']['existing_project_id'] == project['id']
    assert len((await api.get('/api/projects')).json()) == 1


async def test_legacy_symlink_registration_is_resolved(api, project, env):
    path = env['workspaces'] / 'demo'
    alias = env['workspaces'] / 'legacy'
    alias.symlink_to(path, target_is_directory=True)
    with api.app.state.db() as db:
        db.get(Project, project['id']).workspace_path = str(alias)
        db.commit()
    response = await api.post('/api/projects', json={
        'slug': 'other', 'name': 'Other', 'workspace_path': str(path)})
    assert response.status_code == 409, response.text
    assert response.json()['detail']['existing_project_id'] == project['id']


async def test_parallel_alias_registration_only_creates_one(api, env):
    path = env['workspaces'] / 'demo'
    alias = env['workspaces'] / 'alias'
    alias.symlink_to(path, target_is_directory=True)
    responses = await asyncio.gather(*[api.post('/api/projects', json={
        'slug': f'parallel-{index}', 'name': 'Parallel', 'workspace_path': str(folder)})
        for index, folder in enumerate([path, alias])])
    assert sorted(response.status_code for response in responses) == [201, 409]


async def test_same_path_other_account_does_not_block(api, project):
    with api.app.state.db() as db:
        db.get(Project, project['id']).account = 'another-agent'
        db.commit()
    response = await api.post('/api/projects', json={
        'slug': 'separate', 'name': 'Separate', 'workspace_path': project['workspace_path']})
    assert response.status_code == 201, response.text


async def test_remote_identity_is_canonical_and_machine_scoped(api, monkeypatch):
    from acc.server.models import Node

    with api.app.state.db() as db:
        for index, name in enumerate(['box', 'other-box']):
            db.add(Node(id=f'node-{index}', name=name, status='active', info={'accounts': ['acc-agent']}))
        db.commit()
    broker = api.app.state.ctx.broker
    original = broker.call

    async def identify(method, **params):
        if method == 'workspace_identity':
            assert params['account'] == 'acc-agent'
            return {'path': '/repos/canonical', 'allowed': True}
        return await original(method, **params)

    monkeypatch.setattr(broker, 'call', identify)
    first = await api.post('/api/projects', json={
        'slug': 'remote-first', 'name': 'Remote', 'host': 'box', 'workspace_path': '/repos/alias'})
    assert first.status_code == 201, first.text
    assert first.json()['workspace_path'] == '/repos/canonical'
    second = await api.post('/api/projects', json={
        'slug': 'remote-second', 'name': 'Remote', 'host': 'box', 'workspace_path': '/repos/canonical'})
    assert second.status_code == 409, second.text
    assert second.json()['detail']['existing_project_id'] == first.json()['id']
    other = await api.post('/api/projects', json={
        'slug': 'remote-other', 'name': 'Remote', 'host': 'other-box', 'workspace_path': '/repos/canonical'})
    assert other.status_code == 201, other.text
