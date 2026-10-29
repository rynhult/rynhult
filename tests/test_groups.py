"""Group authority must never widen the legacy private-content surface."""
# ruff: noqa: F811
import pytest
from fastapi.routing import APIRoute
from helpers import write_db
from test_auth import live  # noqa: F401 (fixture)
from test_ownership import (
    BODIES,
    MATRIX,
    OWNER_ONLY,
    all_routes,
    fill,
    two,  # noqa: F401 (fixture)
)
from test_users import browser, csrf

from acc.server import auth
from acc.server.models import Group, GroupMembership, User


async def test_groups_are_owner_opt_in_and_memberships_are_revision_fenced(two):
    owner, member = two['owner'], two['member']
    assert (await member.get('/api/groups')).json() == []
    assert (await member.post('/api/groups', json={'name': 'Demo team', 'maintainer_id': 2}, headers=csrf(member))).status_code == 403
    created = await owner.post('/api/groups', json={'name': 'Demo team', 'maintainer_id': 2})
    assert created.status_code == 201, created.text
    group = created.json()
    assert group['revision'] == 1
    assert (await member.get('/api/groups')).json()[0]['role'] == 'maintainer'
    people = (await member.get(f"/api/groups/{group['id']}/people")).json()
    assert {p['username'] for p in people} == {'casey', 'alex'}
    url = f"/api/groups/{group['id']}/members/1"
    added = await member.post(url, json={'role': 'viewer', 'revision': 1}, headers=csrf(member))
    assert added.status_code == 200, added.text
    stale = await member.post(url, json={'role': 'member', 'revision': 1}, headers=csrf(member))
    assert stale.status_code == 409
    last = await member.post(f"/api/groups/{group['id']}/members/2", json={'role': 'viewer', 'revision': 2}, headers=csrf(member))
    assert last.status_code == 409
    assert (await member.post(f"/api/groups/{group['id']}/archive", json={'revision': 2}, headers=csrf(member))).status_code == 403


async def test_named_maintainers_approve_exact_merges_and_legacy_checkout_stays_closed(two):
    owner, maintainer = two['owner'], two['member']
    group = (await owner.post('/api/groups', json={'name': 'Demo team', 'maintainer_id': 2})).json()
    base = f"/api/groups/{group['id']}"
    created = await maintainer.post(base + '/projects', json={'name': 'Demo API', 'slug': 'group-demo', 'owners': [2], 'revision': 1}, headers=csrf(maintainer))
    assert created.status_code == 201, created.text
    project = created.json()
    assert project['owners'] == [2] and project['execution_available']
    for client in (owner, maintainer):
        assert project['id'] not in [p['id'] for p in (await client.get('/api/projects')).json()]
        assert (await client.get(f"/api/projects/{project['id']}/knowledge")).status_code == 404
        assert (await client.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude', 'name': 'Private'}, headers=csrf(client))).status_code == 404
    body = {'revision': 2, 'branch': 'main', 'old_sha': 'a' * 40, 'new_sha': 'b' * 40}
    endpoint = base + f"/projects/{project['id']}/approvals"
    assert (await owner.post(endpoint, json=body)).status_code == 404  # must be named, even an instance owner
    approved = await maintainer.post(endpoint, json=body, headers=csrf(maintainer))
    assert approved.status_code == 201 and approved.json()['applied'] is False
    assert (await maintainer.post(endpoint, json=body, headers=csrf(maintainer))).status_code == 409
    assert (await owner.post(base + '/members/2', json={'role': 'viewer', 'revision': 3})).status_code == 409


async def test_removing_acc_user_requires_replacement_and_removes_group_authority(two):
    owner, maintainer = two['owner'], two['member']
    group = (await owner.post('/api/groups', json={'name': 'Demo team', 'maintainer_id': 2})).json()
    base = f"/api/groups/{group['id']}"
    denied = await owner.post('/api/users/2/remove', json={'resources': 'archive'})
    assert denied.status_code == 409, denied.text
    # Appoint a replacement before removing the original maintainer.
    assert (await owner.post(base + '/members/1', json={'role': 'maintainer', 'revision': 1})).status_code == 200
    removed = await owner.post('/api/users/2/remove', json={'resources': 'archive'})
    assert removed.status_code == 200, removed.text
    assert (await maintainer.get(base)).status_code == 401
    assert [p['id'] for p in (await owner.get(base + '/people')).json()] == [1]


async def test_project_owners_and_placements_are_scoped_and_paths_are_private(two):
    owner, maintainer = two['owner'], two['member']
    group = (await owner.post('/api/groups', json={'name': 'Demo team', 'maintainer_id': 2})).json()
    base = f"/api/groups/{group['id']}"
    project = (await owner.post(base + '/projects', json={'name': 'Demo API', 'slug': 'group-demo', 'owners': [2], 'revision': 1})).json()
    placement_url = base + f"/projects/{project['id']}/placements"
    assert (await maintainer.post(placement_url, json={'user_id': 2, 'revision': 2}, headers=csrf(maintainer))).status_code == 403
    reserved = await owner.post(placement_url, json={'user_id': 2, 'revision': 2})
    assert reserved.status_code == 201 and reserved.json()['trust'] == 'pending'
    placements = (await maintainer.get(placement_url)).json()
    assert len(placements) == 1 and placements[0]['person'] == 'alex'
    assert placements[0]['sync_status'] == 'unprovisioned'
    assert (await owner.post(base + '/members/1', json={'role': 'maintainer', 'revision': 3})).status_code == 200
    owners_url = base + f"/projects/{project['id']}/owners"
    assert (await owner.post(owners_url, json={'owners': [1], 'revision': 4})).status_code == 200
    assert (await owner.post(base + '/members/2', json={'role': 'remove', 'revision': 5})).status_code == 200
    assert (await maintainer.get(placement_url)).status_code == 404
    assert (await maintainer.get(base)).status_code == 404
    assert (await maintainer.get('/api/groups')).json() == []


async def test_new_personal_sessions_keep_an_immutable_routing_snapshot(two):
    from helpers import write_db

    from acc.server.models import Session
    with write_db(two['owner'].app.state.db) as s:
        session = s.get(Session, two['session']['id'])
        assert session.placement_id is not None
        original = (session.host, session.cwd, session.run_as)
        session.project.host = 'later-machine'
        session.project.workspace_path = '/later/path'
        s.commit()
        assert (session.host, session.cwd, session.run_as) == original


@pytest.fixture
async def grouped(two):
    owner = two['owner']
    group = (await owner.post('/api/groups', json={'name': 'Demo team', 'maintainer_id': 2})).json()
    project = (await owner.post(f"/api/groups/{group['id']}/projects", json={
        'name': 'Group project', 'slug': 'group-demo', 'owners': [2], 'revision': 1})).json()
    return {**two, 'group': group, 'group_project': project}


@pytest.fixture(params=['preview on', 'preview off'])
def preview_switch(request, monkeypatch):
    """The orchestrator feature's routes are in MATRIX: on, they refuse group roles like any other; off, they're absent."""
    monkeypatch.setenv('ACC_ORCHESTRATOR', '1' if request.param == 'preview on' else '0')
    return request.param


@pytest.mark.parametrize('role', ['maintainer', 'member', 'viewer', 'nonmember', 'removed'])
async def test_group_roles_never_open_any_legacy_private_route(preview_switch, grouped, role, env):
    """Every registered resource route is enumerated by test_ownership's route-discovery test.

    Removing the Project gate, widening session scope, or letting group roles become instance
    powers exposes real private data here, including exports, Files, wiki and Preview.
    """
    from helpers import control, wait_for
    w = grouped
    control(env, w['session']['id'], 'say groupprivatecanary')

    async def searchable():
        return (await w['owner'].get('/api/search', params={'q': 'groupprivatecanary'})).json()['results']

    found = await wait_for(searchable)
    assert found[0]['session']['id'] == w['session']['id']
    with write_db(w['owner'].app.state.db) as s:
        user = User(username='sam', role='member', account='sam-agent', password_hash='unused', totp_secret='unused')
        s.add(user)
        s.flush()
        token, csrf_token = auth.create_session(s, user, 'test')
        if role not in ('nonmember', 'removed'):
            s.add(GroupMembership(group_id=w['group']['id'], user_id=user.id, role=role))
        elif role == 'removed':
            s.add(GroupMembership(group_id=w['group']['id'], user_id=user.id, role='member'))
            s.flush()
            s.delete(s.get(GroupMembership, (w['group']['id'], user.id)))
            s.get(Group, w['group']['id']).revision += 1
        s.commit()
    client = browser(w['owner'])
    client.cookies.set('acc_session', token)
    client.cookies.set('acc_csrf', csrf_token)
    try:
        # Use the group Project's real id, with an unrelated person's private session and Inbox.
        bound = {**w, 'project': w['group_project']}
        registered = {(m, prefix + r.path) for prefix, r in all_routes(w['owner'].app.routes)
                      if isinstance(r, APIRoute) for m in r.methods}
        for (method, path), expected in MATRIX.items():
            if expected is None or path.startswith('/api/groups/'):
                continue
            from acc.server.admin_features import path_feature
            preview_disabled = preview_switch == 'preview off' and path_feature(fill(path, bound)) == 'orchestrator'
            if (method, path) not in registered or preview_disabled:
                expected = 404  # a switched-off preview's route is absent, whatever the method
            url = fill(path, bound)
            query = {'path': 'README.md'} if path.endswith(('/doc', '/page', '/attachment')) else None
            if path.endswith('/compare'):
                query = {'left': 'a', 'right': 'b'}
            body = next((b for k, b in BODIES.items() if url.endswith(k)), {})
            response = await client.request(method, url, params=query, json=body if method != 'GET' else None, headers=csrf(client))
            assert response.status_code == expected, (role, method, path, response.status_code, response.text[:200])
        for method, path in OWNER_ONLY:
            response = await client.request(method, path, json={} if method != 'GET' else None, headers=csrf(client))
            switched_off = preview_switch == 'preview off' and path_feature(path) == 'orchestrator'
            assert response.status_code in ((404,) if switched_off else (403, 422)), (role, path, response.status_code)
        for path in ('/api/sessions', '/api/attention', '/api/publish', '/api/workspaces', '/api/projects'):
            assert (await client.get(path)).json() == [], (role, path)
        assert (await client.get('/api/search', params={'q': 'groupprivatecanary'})).json()['results'] == []
        shared = await client.get(f"/api/groups/{w['group']['id']}/projects")
        assert shared.status_code == (404 if role in ('nonmember', 'removed') else 200)
        if shared.status_code == 200:
            assert shared.json() == [w['group_project']]
            assert 'workspace_path' not in shared.text
        for path in ('people', 'audit'):
            assert (await client.get(f"/api/groups/{w['group']['id']}/{path}")).status_code == (200 if role == 'maintainer' else 404)
    finally:
        await client.aclose()


async def test_membership_updates_serialize_and_invalidate_stale_requests(grouped):
    import asyncio
    owner, group = grouped['owner'], grouped['group']
    url = f"/api/groups/{group['id']}/members/1"
    results = await asyncio.gather(*(owner.post(url, json={'role': role, 'revision': 2}) for role in ('member', 'viewer')))
    assert sorted(r.status_code for r in results) == [200, 409]
    current = (await owner.get(f"/api/groups/{group['id']}")).json()
    assert current['revision'] == 3


async def test_key_grants_need_explicit_key_owner_consent_and_can_be_revoked(grouped):
    from acc.server.models import GroupKeyGrant, VaultKey
    owner, maintainer, group = grouped['owner'], grouped['member'], grouped['group']
    with write_db(owner.app.state.db) as s:
        s.add(VaultKey(id='demo-key', name='Private canary', variable='DEMO_KEY', ciphertext='sealed', owner='alex'))
        s.commit()
    url = f"/api/groups/{group['id']}/key-grants"
    body = {'revision': 2, 'key_id': 'demo-key', 'host': 'local'}
    assert (await owner.post(url, json=body)).status_code == 404
    granted = await maintainer.post(url, json=body, headers=csrf(maintainer))
    assert granted.status_code == 201 and granted.json()['delivery_available'] is False
    assert (await owner.post(url + f"/{granted.json()['id']}/revoke", json={'revision': 3})).status_code == 404
    revoked = await maintainer.post(url + f"/{granted.json()['id']}/revoke", json={'revision': 3}, headers=csrf(maintainer))
    assert revoked.status_code == 200, revoked.text
    with write_db(owner.app.state.db) as s:
        assert s.get(GroupKeyGrant, granted.json()['id']) is None
    audit = (await maintainer.get(f"/api/groups/{group['id']}/audit")).text
    assert 'Private canary' not in audit and 'sealed' not in audit


async def test_instance_owner_cannot_reach_live_group_checkout_through_any_project_route(grouped):
    from test_auth import cookie_client
    owner = cookie_client(grouped['owner'])
    for (method, path), _ in MATRIX.items():
        if not path.startswith('/api/projects/{project_id}/'):
            continue
        url = fill(path, {**grouped, 'project': grouped['group_project']})
        body = next((b for k, b in BODIES.items() if url.endswith(k)), {})
        response = await owner.request(method, url, params={'path': 'README.md'}, json=body if method != 'GET' else None, headers=csrf(owner))
        assert response.status_code == 404, (method, path, response.status_code, response.text[:200])


@pytest.mark.parametrize('role', ['maintainer', 'member', 'viewer', 'nonmember', 'removed'])
async def test_group_role_never_opens_another_persons_terminal(grouped, live, role):
    import websockets
    with write_db(grouped['owner'].app.state.db) as s:
        membership = s.get(GroupMembership, (grouped['group']['id'], 2))
        if role in ('nonmember', 'removed'):
            s.delete(membership)
        else:
            membership.role = role
        s.commit()
    url = f"{live}/api/sessions/{grouped['session']['id']}/tty"
    cookie = {'Cookie': f"acc_session={grouped['member'].cookies.get('acc_session')}", 'Origin': 'http://127.0.0.1:18740'}
    with pytest.raises(websockets.InvalidStatus) as refused:
        async with websockets.connect(url, additional_headers=cookie):
            pass
    assert refused.value.response.status_code == 403


@pytest.mark.parametrize('role', ['maintainer', 'member', 'viewer', 'nonmember', 'removed'])
async def test_group_management_routes_follow_role_matrix(grouped, role):
    actor, owner = grouped['member'], grouped['owner']
    group_id, project_id = grouped['group']['id'], grouped['group_project']['id']
    with write_db(owner.app.state.db) as s:
        membership = s.get(GroupMembership, (group_id, 2))
        if role in ('nonmember', 'removed'):
            s.delete(membership)
        else:
            membership.role = role
        s.commit()
    base = f'/api/groups/{group_id}'
    writes = [
        ('/members/1', {'role': 'viewer'}),
        ('/projects', {'name': 'Another', 'slug': 'another', 'owners': [2]}),
        (f'/projects/{project_id}/owners', {'owners': [2]}),
        (f'/projects/{project_id}/approvals', {'branch': 'main', 'old_sha': 'a' * 40, 'new_sha': 'b' * 40}),
        (f'/projects/{project_id}/archive', {}),
        ('/unpause', {}),
    ]
    for path, body in writes:
        revision = (await owner.get(base)).json()['revision']
        response = await actor.post(base + path, json={'revision': revision, **body}, headers=csrf(actor))
        expected = (201 if path in ('/projects', f'/projects/{project_id}/approvals') else 200) if role == 'maintainer' else 404
        assert response.status_code == expected, (role, path, response.text)
    for path, body in [('/archive', {}), (f'/projects/{project_id}/placements', {'user_id': 2})]:
        response = await actor.post(base + path, json={'revision': 1, **body}, headers=csrf(actor))
        assert response.status_code == 403
    launch = await actor.post('/api/sessions', json={'project_id': project_id, 'provider': 'claude', 'name': 'Refused'}, headers=csrf(actor))
    assert launch.status_code == 404


async def test_people_sharing_a_trusted_owner_account_get_distinct_clone_reservations(grouped):
    owner = grouped['owner']
    base = f"/api/groups/{grouped['group']['id']}"
    with write_db(owner.app.state.db) as s:
        other = User(username='blake', role='owner', password_hash='unused', totp_secret='unused')
        s.add(other)
        s.flush()
        other_id = other.id
        s.add(GroupMembership(group_id=grouped['group']['id'], user_id=other_id, role='member'))
        s.add(GroupMembership(group_id=grouped['group']['id'], user_id=1, role='member'))
        s.get(Group, grouped['group']['id']).revision += 1
        s.commit()
    url = base + f"/projects/{grouped['group_project']['id']}/placements"
    for user_id in (1, other_id):
        revision = (await owner.get(base)).json()['revision']
        response = await owner.post(url, json={'revision': revision, 'user_id': user_id})
        assert response.status_code == 201, response.text
    placements = (await owner.get(url)).json()
    assert len(placements) == 2
    assert placements[0]['account'] == placements[1]['account']
    assert placements[0]['path'] != placements[1]['path']


async def test_retiring_a_person_never_reuses_their_reserved_clone_path(grouped):
    owner = grouped['owner']
    group_id = grouped['group']['id']
    base = f'/api/groups/{group_id}'
    with write_db(owner.app.state.db) as s:
        person = User(username='blake', role='owner', password_hash='unused', totp_secret='unused')
        s.add(person)
        s.flush()
        old_id = person.id
        s.add(GroupMembership(group_id=group_id, user_id=old_id, role='member'))
        s.get(Group, group_id).revision += 1
        s.commit()
    url = base + f"/projects/{grouped['group_project']['id']}/placements"
    revision = (await owner.get(base)).json()['revision']
    assert (await owner.post(url, json={'revision': revision, 'user_id': old_id})).status_code == 201
    old_path = (await owner.get(url)).json()[0]['path']
    assert (await owner.post(f'/api/users/{old_id}/remove', json={'resources': 'archive'})).status_code == 200
    with write_db(owner.app.state.db) as s:
        person = User(username='kris', role='owner', password_hash='unused', totp_secret='unused')
        s.add(person)
        s.flush()
        assert person.id == old_id  # SQLite may recycle numeric IDs; retired usernames never recycle.
        s.add(GroupMembership(group_id=group_id, user_id=person.id, role='member'))
        s.get(Group, group_id).revision += 1
        s.commit()
    revision = (await owner.get(base)).json()['revision']
    assert (await owner.post(url, json={'revision': revision, 'user_id': old_id})).status_code == 201
    new_path = next(p['path'] for p in (await owner.get(url)).json() if p['person'] == 'kris')
    assert new_path != old_path


@pytest.mark.parametrize('role', ['owner', 'maintainer', 'member', 'viewer', 'nonmember', 'removed'])
async def test_no_group_or_instance_role_substitutes_for_a_node_signature(grouped, live, role):
    import base64
    import json

    import websockets
    from test_multihost import enroll

    _, node_id = await enroll(grouped['owner'])
    client = grouped['owner'] if role == 'owner' else grouped['member']
    if role != 'owner':
        with write_db(grouped['owner'].app.state.db) as s:
            membership = s.get(GroupMembership, (grouped['group']['id'], 2))
            if role in ('nonmember', 'removed'):
                s.delete(membership)
            else:
                membership.role = role
            s.commit()
    cookies = {'Cookie': f"acc_session={client.cookies.get('acc_session')}"}
    async with websockets.connect(f'{live}/api/nodes/connect', additional_headers=cookies) as ws:
        assert json.loads(await ws.recv())['t'] == 'challenge'
        await ws.send(json.dumps({'t': 'hello', 'node_id': node_id,
                                  'signature': base64.b64encode(b'\0' * 64).decode()}))
        with pytest.raises(websockets.ConnectionClosed) as refused:
            await ws.recv()
        assert refused.value.rcvd.code == 4401
