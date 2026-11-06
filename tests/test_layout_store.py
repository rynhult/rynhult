"""Personal presentation records never grant resource access or silently overwrite another device."""
import asyncio

import pytest
from sqlalchemy import delete, select
from test_auth import cookie_client, do_setup
from test_users import browser, csrf, join

from acc.server.models import AuditLog, Project, User

LAYOUT = {'schema_version': 2, 'views': [{'id': 'main', 'kind': 'terminal', 'resource': {'kind': 'session', 'id': 'missing'}}],
          'columns': [{'tabs': ['main'], 'active': 'main', 'width': 1}], 'focused': 0, 'filled': None,
          'sidebar': {'collapsed': False}, 'header': {'compact': True}}


@pytest.fixture
async def personal(api, env, project):
    await do_setup(api, env)
    cookie_client(api)
    api.headers.update(csrf(api))
    return api, project, f'/api/projects/{project["id"]}/layouts/desktop/wide'


async def test_cas_reset_unknown_fields_and_unavailable_resources(personal):
    api, project, url = personal
    first = (await api.get(url)).json()
    assert first['revision'] == 0 and first['layout'] is None
    response = await api.put(url, json={'expected_revision': 0, 'layout': {**LAYOUT, 'draft': 'never store'}})
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved['revision'] == 1 and saved['layout'] == LAYOUT
    assert saved['resources'] == {'main': {'available': False}}
    assert saved['scope']['project_id'] == project['id']
    assert saved['scope']['installation'] and saved['scope']['person']
    conflict = await api.put(url, json={'expected_revision': 0, 'layout': LAYOUT})
    assert conflict.status_code == 409 and conflict.json()['current'] == saved
    reset = await api.post(url + '/reset', json={'expected_revision': 1})
    assert reset.status_code == 200 and reset.json()['revision'] == 2 and reset.json()['layout'] is None
    assert (await api.put(url, json={'expected_revision': 1, 'layout': LAYOUT})).status_code == 409
    with api.app.state.db() as db:
        events = db.scalars(select(AuditLog).where(AuditLog.action.like('layout.%'))).all()
        assert len(events) == 1 and events[0].action == 'layout.reset'
        assert 'views' not in str(events[0].detail)


async def test_simultaneous_creates_and_updates_have_one_winner(personal):
    api, _, url = personal
    for revision in (0, 1):
        replies = await asyncio.gather(*(api.put(url, json={'expected_revision': revision, 'layout': LAYOUT}) for _ in range(2)))
        assert sorted(r.status_code for r in replies) == [200, 409]
        assert (await api.get(url)).json()['revision'] == revision + 1


async def test_isolation_owner_cannot_select_member_layout(personal, member_accounts):
    api, _, url = personal
    invite = await api.post('/api/users/invites', json={'account': 'dana-agent'}, headers=csrf(api))
    async with browser(api) as member:
        assert (await join(member, invite.json()['link'], 'alex')).status_code == 200
        member.headers.update(csrf(member))
        assert (await member.get(url)).status_code == 404
        made = await member.post('/api/projects/new', json={'name': 'Personal layout'})
        assert made.status_code == 201, made.text
        own = f'/api/projects/{made.json()["id"]}/layouts/phone/narrow'
        stored = await member.put(own, json={'expected_revision': 0, 'layout': LAYOUT})
        assert stored.status_code == 200, stored.text
        unauthorized_ref = {'schema_version': 2, 'views': [{'id': 'foreign', 'kind': 'page',
                            'resource': {'kind': 'project', 'id': personal[1]['id']}}], 'focused': 0, 'filled': None,
                            'columns': [{'tabs': ['foreign'], 'active': 'foreign', 'width': 1}]}
        checked = await member.put(own, json={'expected_revision': 1, 'layout': unauthorized_ref})
        assert checked.json()['resources'] == {'foreign': {'available': False}}
        # Owner may access the project, but only sees their OWN empty layout.
        owner_record = (await api.get(own + '?person=alex')).json()
        assert owner_record['revision'] == 0 and owner_record['layout'] is None
        assert owner_record['scope']['person'] != stored.json()['scope']['person']
        with api.app.state.db() as db:
            user = db.scalar(select(User).where(User.username == 'alex'))
            # FK deletion is tested without unrelated account-removal resources.
            from acc.server.models import AuthSession, PersonalLayout
            db.execute(delete(AuthSession).where(AuthSession.user_id == user.id))
            db.delete(user)
            db.commit()
            assert not db.scalars(select(PersonalLayout).where(PersonalLayout.owner == 'alex')).all()


async def test_api_token_and_anonymous_cannot_read_or_write_layouts(api, project):
    url = f'/api/projects/{project["id"]}/layouts/desktop/wide'
    assert (await api.get(url)).status_code == 403
    assert (await api.put(url, json={'expected_revision': 0, 'layout': LAYOUT})).status_code == 403
    async with browser(api) as anonymous:
        assert (await anonymous.get(url)).status_code == 401


@pytest.mark.parametrize('layout', [
    {**LAYOUT, 'schema_version': 1}, {**LAYOUT, 'schema_version': 3}, {**LAYOUT, 'views': LAYOUT['views'] * 33},
    {**LAYOUT, 'views': [{'id': 'main', 'kind': 'page', 'resource': {'kind': 'file', 'id': '/etc/passwd'}}]},
    {**LAYOUT, 'views': [{'id': 'src/secret.py', 'kind': 'page'}], 'columns': [{'tabs': ['src/secret.py'], 'active': 'src/secret.py', 'width': 1}]},
    {**LAYOUT, 'views': [{'id': 'main', 'kind': 'file', 'file_ref': 'src/secret.py'}]},
    {**LAYOUT, 'views': [{'id': 'main', 'kind': 'file'}]},
    {**LAYOUT, 'views': [{'id': 'main', 'kind': 'macro'}]},
    {**LAYOUT, 'views': [{'id': 'main', 'kind': 'terminal', 'resource': {'kind': 'project', 'id': 1}}]},
    {**LAYOUT, 'views': [{'id': 'main', 'kind': 'page', 'tab': 'files?file=a'}]},
    {**LAYOUT, 'columns': [{'tabs': ['main'], 'active': 'main', 'width': float('inf')}]},
    {**LAYOUT, 'columns': [{'tabs': ['main'], 'active': 'main', 'width': 10**500}]},
    {**LAYOUT, 'columns': [{'tabs': ['main'], 'active': 'main', 'width': .5}]},
    {**LAYOUT, 'columns': [{'tabs': [], 'active': None, 'width': 1}]},
    {**LAYOUT, 'columns': [{'tabs': ['main'], 'active': None, 'width': 1}]},
    {**LAYOUT, 'columns': [{'tabs': ['main'], 'active': 'other', 'width': 1}]},
    {**LAYOUT, 'columns': [{'tabs': ['main', 'main'], 'active': 'main', 'width': 1}]},
    {**LAYOUT, 'columns': [{'tabs': [], 'active': None, 'width': .25}] * 4},
    {**LAYOUT, 'columns': []}, {**LAYOUT, 'focused': 1}, {**LAYOUT, 'filled': 'other'},
])
async def test_reject_invalid_layout(personal, layout):
    api, _, url = personal
    # Infinity cannot be encoded by httpx's strict JSON encoder.
    import json
    response = await api.put(url, content=json.dumps({'expected_revision': 0, 'layout': layout}),
                             headers={'content-type': 'application/json'})
    assert response.status_code == 422, response.text


async def test_body_bound_and_project_cascade(personal):
    api, project, url = personal
    assert (await api.put(url, json={'expected_revision': 0, 'layout': LAYOUT, 'unknown': 'x' * 40000})).status_code == 413
    assert (await api.put(url, json={'expected_revision': 0, 'layout': LAYOUT})).status_code == 200
    from acc.server.models import PersonalLayout
    with api.app.state.db() as db:
        db.delete(db.get(Project, project['id']))
        db.commit()
        assert not db.scalars(select(PersonalLayout)).all()
    assert (await api.get(url)).status_code == 404


async def test_private_workspace_and_current_authorization_resolution(personal):
    api, project, url = personal
    with api.app.state.db() as db:
        db.get(Project, project['id']).private_workspace = True
        db.commit()
    layout = {'schema_version': 2, 'views': [{'id': 'project', 'kind': 'page', 'transcript': 'drop', 'resource': {'kind': 'project', 'id': project['id'],
              'token': 'drop'}}], 'columns': [{'tabs': ['project'], 'active': 'project', 'width': 1, 'note': 'drop'}], 'focused': 0, 'filled': None,
              'sidebar': {'collapsed': True, 'credential': 'drop'}}
    saved = await api.put(url, json={'expected_revision': 0, 'layout': layout})
    assert saved.status_code == 200, saved.text
    assert saved.json()['resources']['project']['available'] is True
    assert 'drop' not in saved.text
    with api.app.state.db() as db:
        db.get(Project, project['id']).archived = True
        db.commit()
    assert (await api.get(url)).json()['resources']['project']['available'] is False


async def test_project_deletion_route_removes_layouts(personal):
    api, project, url = personal
    assert (await api.put(url, json={'expected_revision': 0, 'layout': LAYOUT})).status_code == 200
    response = await api.post(f'/api/projects/{project["id"]}/delete', json={'confirm': project['slug']})
    assert response.status_code == 200, response.text
    from acc.server.models import PersonalLayout
    with api.app.state.db() as db:
        assert not db.scalars(select(PersonalLayout)).all()


async def test_device_scope_and_reset_cas(personal):
    api, _, url = personal
    assert (await api.put(url, json={'expected_revision': 0, 'layout': LAYOUT})).status_code == 200
    assert (await api.get(url.replace('desktop', 'phone'))).json()['revision'] == 0
    assert (await api.get(url.replace('wide', 'narrow'))).json()['revision'] == 0
    assert (await api.get(url.replace('desktop', 'arbitrary'))).status_code == 422
    replies = await asyncio.gather(*(api.post(url + '/reset', json={'expected_revision': 1}) for _ in range(2)))
    assert sorted(r.status_code for r in replies) == [200, 409]
