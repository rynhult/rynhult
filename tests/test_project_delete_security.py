"""RC1 batch 7 proofs: all accounts, files and publishing services are fixture-owned."""
from pathlib import Path

import pytest
from sqlalchemy import select
from test_publish import world  # noqa: F401 (fixture)

from acc.broker import ops
from acc.server.models import PortReservation, Project, PublishRequest


async def empty_project(api, name):
    response = await api.post('/api/projects/new', json={'name': name, 'source': 'empty'})
    assert response.status_code == 201, response.text
    return response.json()


def linked_tree(env, project, suffix):
    made = ops.make_worktree(env['broker'], project['workspace_path'], str(env['workspaces']),
                             f'{project["slug"]}-{suffix}', f'acc/{suffix}')
    return Path(made['path'])


@pytest.mark.parametrize('live_foreign_session', [False, True])
async def test_b7_1_forged_git_discovery_cannot_delete_foreign_worktree(api, env, live_foreign_session):
    a = await empty_project(api, 'Project a')
    b = await empty_project(api, 'Project b')
    own = linked_tree(env, a, 'own-12345678')
    if live_foreign_session:
        response = await api.post('/api/sessions', json={'project_id': b['id'], 'provider': 'claude',
                                                        'name': 'Foreign live session', 'worktree': True})
        assert response.status_code == 201, response.text
        foreign = Path(response.json()['worktree'])
    else:
        foreign = linked_tree(env, b, 'foreign-12345678')
    marker = foreign / 'keep.txt'
    marker.write_text('belongs to B')
    admin = Path(a['workspace_path']) / '.git/worktrees' / own.name
    (admin / 'gitdir').write_text(str(foreign / '.git') + '\n')
    address = f'/api/projects/{a["id"]}/delete'
    preview = await api.get(address)
    assert preview.status_code == 409, preview.text
    result = await api.post(address, json={'confirm': a['slug']})
    assert result.status_code == 409, result.text
    assert marker.read_text() == 'belongs to B'
    assert own.is_dir() and Path(a['workspace_path']).is_dir()


async def test_b7_1_foreign_session_reference_blocks_even_reciprocally_forged_metadata(api, env):
    a = await empty_project(api, 'Project a')
    b = await empty_project(api, 'Project b')
    own = linked_tree(env, a, 'own-12345678')
    response = await api.post('/api/sessions', json={'project_id': b['id'], 'provider': 'claude',
                                                    'name': 'Foreign live session', 'worktree': True})
    assert response.status_code == 201, response.text
    foreign = Path(response.json()['worktree'])
    marker = foreign / 'keep.txt'
    marker.write_text('still B')
    admin = Path(a['workspace_path']) / '.git/worktrees' / own.name
    (admin / 'gitdir').write_text(str(foreign / '.git') + '\n')
    (foreign / '.git').write_text(f'gitdir: {admin}\n')
    result = await api.post(f'/api/projects/{a["id"]}/delete', json={'confirm': a['slug']})
    assert result.status_code == 409, result.text
    assert marker.read_text() == 'still B'


async def test_b7_3_owner_publish_during_checkout_removal_is_refused(api, env, monkeypatch):
    p = await empty_project(api, 'Published project')
    with api.app.state.db() as db:
        db.add(PortReservation(port=8134, project_id=p['id']))
        db.commit()
    broker = api.app.state.supervisor.broker
    original = broker.call
    attempted = []
    async def interleave(method, **kwargs):
        if method == 'delete_project_checkout':
            attempted.append(await api.post('/api/publish', json={'host': 'demo.example.com', 'port': 8134,
                                                                 'access': 'public'}))
        return await original(method, **kwargs)
    monkeypatch.setattr(broker, 'call', interleave)
    result = await api.post(f'/api/projects/{p["id"]}/delete', json={'confirm': p['slug']})
    assert result.status_code == 200, result.text
    assert len(attempted) == 1
    assert attempted[0].status_code == 409, attempted[0].text
    with api.app.state.db() as db:
        assert db.scalar(select(PublishRequest).where(PublishRequest.host == 'demo.example.com')) is None


@pytest.mark.parametrize('status', ['pending_approval', 'approved', 'applying', 'applied'])
async def test_b7_3_legacy_projectless_publication_on_reserved_port_blocks_delete(api, env, status):
    p = await empty_project(api, 'Legacy published project')
    with api.app.state.db() as db:
        db.add(PortReservation(port=8134, project_id=p['id']))
        db.add(PublishRequest(id='legacy-site', action='publish', host='demo.example.com', port=8134,
                              status=status, requested_by='api'))
        db.commit()
    for response in (await api.get(f'/api/projects/{p["id"]}/delete'),
                     await api.post(f'/api/projects/{p["id"]}/delete', json={'confirm': p['slug']})):
        assert response.status_code == 409, response.text
    with api.app.state.db() as db:
        assert db.get(Project, p['id']) is not None
    assert Path(p['workspace_path']).is_dir()


@pytest.mark.parametrize('nested', [False, True])
async def test_b7_1_registered_project_reference_protects_discovered_worktree(api, env, nested):
    a = await empty_project(api, 'Project a')
    target = linked_tree(env, a, 'own-12345678')
    folder = target / 'registered-child' if nested else target
    folder.mkdir(exist_ok=True)
    marker = folder / 'keep.txt'
    marker.write_text('registered elsewhere')
    registered = await api.post('/api/projects', json={'slug': 'registered-b', 'name': 'Registered B',
                                                      'workspace_path': str(folder)})
    assert registered.status_code == 201, registered.text
    result = await api.post(f'/api/projects/{a["id"]}/delete', json={'confirm': a['slug']})
    assert result.status_code == 409, result.text
    assert marker.read_text() == 'registered elsewhere'


async def test_b7_3_owner_request_derives_and_persists_reserved_project(api, env):
    p = await empty_project(api, 'Port association')
    with api.app.state.db() as db:
        db.add(PortReservation(port=8134, project_id=p['id']))
        db.commit()
    response = await api.post('/api/publish', json={'host': 'linked.example.com', 'port': 8134, 'access': 'public'})
    assert response.status_code == 201, response.text
    assert response.json()['project_id'] == p['id']
    with api.app.state.db() as db:
        assert db.get(PublishRequest, response.json()['id']).project_id == p['id']


async def test_b7_3_owner_request_respects_project_operation_lock(api, env):
    p = await empty_project(api, 'Busy port association')
    with api.app.state.db() as db:
        db.add(PortReservation(port=8134, project_id=p['id']))
        db.commit()
    async with api.app.state.supervisor.workspace_operations.hold(p['workspace_path']):
        response = await api.post('/api/publish', json={'host': 'busy.example.com', 'port': 8134, 'access': 'public'})
        assert response.status_code == 409, response.text
    with api.app.state.db() as db:
        assert db.scalar(select(PublishRequest).where(PublishRequest.host == 'busy.example.com')) is None


async def test_b7_3_applied_owner_site_blocks_delete_until_unpublished(api, env, world):  # noqa: F811
    from helpers import wait_for
    from test_publish import status_is

    p = await empty_project(api, 'Live published project')
    with api.app.state.db() as db:
        db.add(PortReservation(port=8134, project_id=p['id']))
        db.commit()
    response = await api.post('/api/publish', json={'host': 'live.example.com', 'port': 8134, 'access': 'public'})
    assert response.status_code == 201, response.text
    request = response.json()
    assert request['project_id'] == p['id']
    await wait_for(lambda: status_is(api, request['id'], 'applied'))
    address = f'/api/projects/{p["id"]}/delete'
    assert (await api.post(address, json={'confirm': p['slug']})).status_code == 409
    assert Path(p['workspace_path']).is_dir()
    unpublish = await api.post(f'/api/publish/{request["id"]}/unpublish')
    assert unpublish.status_code == 200, unpublish.text
    await wait_for(lambda: status_is(api, unpublish.json()['id'], 'removed'))
    result = await api.post(address, json={'confirm': p['slug']})
    assert result.status_code == 200, result.text
    assert not Path(p['workspace_path']).exists()


async def test_b7_1_nested_registration_cannot_claim_a_target_during_removal(api, env, monkeypatch):
    a = await empty_project(api, 'Project a')
    target = linked_tree(env, a, 'own-12345678')
    child = target / 'child'
    child.mkdir()
    broker = api.app.state.supervisor.broker
    original = broker.call
    attempts = []
    async def interleave(method, **kwargs):
        if method == 'delete_project_checkout':
            attempts.append(await api.post('/api/projects', json={'slug': 'new-b', 'name': 'New B',
                                                                  'workspace_path': str(child)}))
        return await original(method, **kwargs)
    monkeypatch.setattr(broker, 'call', interleave)
    response = await api.post(f'/api/projects/{a["id"]}/delete', json={'confirm': a['slug']})
    assert response.status_code == 200, response.text
    assert len(attempts) == 1 and attempts[0].status_code == 409, [r.text for r in attempts]


async def test_b7_1_independent_checkout_needs_recorded_session_authority(api, env):
    from acc.server.models import LaunchSpec, Session

    a = await empty_project(api, 'Private checkout project')
    made = ops.make_private_checkout(env['broker'], a['workspace_path'], str(env['workspaces']),
                                    'private-12345678', 'acc/private-12345678')
    target = Path(made['path'])
    (target / 'dirty.txt').write_text('confirmed loss')
    with api.app.state.db() as db:
        spec = LaunchSpec(provider='claude', inputs={}, files={}, digest='test-private')
        db.add(spec)
        db.flush()
        db.add(Session(id='private-kept', project_id=a['id'], provider='claude', name='Private checkout',
                       tmux_name='acc-private-kept', launch_spec_id=spec.id, run_as=a['account'],
                       desired_state='stopped', observed_state='stopped', worktree=str(target), container=True))
        db.commit()
    response = await api.post(f'/api/projects/{a["id"]}/delete', json={'confirm': a['slug']})
    assert response.status_code == 200, response.text
    assert not target.exists()


async def test_b7_1_discovery_cannot_authorize_an_independent_foreign_checkout(api, env):
    a = await empty_project(api, 'Project a')
    b = await empty_project(api, 'Project b')
    own = linked_tree(env, a, 'own-12345678')
    made = ops.make_private_checkout(env['broker'], b['workspace_path'], str(env['workspaces']),
                                    'foreign-private-12345678', 'acc/private-12345678')
    target = Path(made['path'])
    marker = target / 'keep.txt'
    marker.write_text('belongs to B')
    admin = Path(a['workspace_path']) / '.git/worktrees' / own.name
    (admin / 'gitdir').write_text(str(target / '.git') + '\n')
    response = await api.post(f'/api/projects/{a["id"]}/delete', json={'confirm': a['slug']})
    assert response.status_code == 409, response.text
    assert marker.read_text() == 'belongs to B'


async def test_b7_3_deriving_port_identity_does_not_authorize_a_foreign_member(api, env, member_accounts):
    from test_auth import cookie_client, do_setup
    from test_users import browser, csrf, join

    p = await empty_project(api, 'Owner port')
    with api.app.state.db() as db:
        db.add(PortReservation(port=8134, project_id=p['id']))
        db.commit()
    await do_setup(api, env)
    owner = cookie_client(api)
    invite = await owner.post('/api/users/invites', json={'role': 'member', 'account': 'dana-agent'}, headers=csrf(owner))
    assert invite.status_code == 201, invite.text
    async with browser(api) as member:
        assert (await join(member, invite.json()['link'], 'alex')).status_code == 200
        response = await member.post('/api/publish', json={'host': 'foreign.example.com', 'port': 8134,
                                                          'access': 'public'}, headers=csrf(member))
        assert response.status_code == 409, response.text
    with api.app.state.db() as db:
        assert db.scalar(select(PublishRequest).where(PublishRequest.host == 'foreign.example.com')) is None


async def test_b7_1_older_broker_identity_cannot_authorize_deletion(api, env, monkeypatch):
    a = await empty_project(api, 'Broker version project')
    broker = api.app.state.supervisor.broker
    original = broker.call
    async def older_identity(method, **kwargs):
        result = await original(method, **kwargs)
        if method == 'project_delete_identity':
            return {key: value for key, value in result.items() if key != 'version'}
        return result
    monkeypatch.setattr(broker, 'call', older_identity)
    response = await api.post(f'/api/projects/{a["id"]}/delete', json={'confirm': a['slug']})
    assert response.status_code == 409, response.text
    assert Path(a['workspace_path']).is_dir()
