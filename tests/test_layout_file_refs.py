"""Schema 2: working columns and opaque file references, resolved through current authorization."""
import pytest
from sqlalchemy import select
from test_auth import cookie_client, do_setup
from test_users import browser, csrf, join

from acc.server import layout_store
from acc.server.models import LaunchSpec, LayoutFileRef, PersonalLayout, Project, Session, User


def file_layout(reference, *more):
    views = [{'id': 'f-' + reference, 'kind': 'file', 'file_ref': reference}, *more]
    ids = [view['id'] for view in views]
    return {'schema_version': 2, 'views': views, 'columns': [{'tabs': ids, 'active': ids[0], 'width': 1}], 'focused': 0, 'filled': None}


@pytest.fixture
async def personal(api, env, project):
    await do_setup(api, env)
    cookie_client(api)
    api.headers.update(csrf(api))
    root = f'/api/projects/{project["id"]}/layouts'
    return api, project, root, root + '/desktop/wide'


async def mint(client, root, project, path='docs/secret-plan.md'):
    response = await client.post(root + '/file-refs', json={'location': 'project', 'id': str(project['id']), 'path': path})
    assert response.status_code == 200, response.text
    return response.json()['file_ref']


async def test_a_file_view_stores_a_reference_and_resolves_only_in_the_response(personal):
    api, project, root, url = personal
    reference = await mint(api, root, project)
    assert await mint(api, root, project) == reference  # stable between windows
    assert 'secret-plan' not in reference
    saved = await api.put(url, json={'expected_revision': 0, 'layout': file_layout(reference)})
    assert saved.status_code == 200, saved.text
    assert saved.json()['resources']['f-' + reference] == {'available': True, 'file': {
        'location': 'project', 'id': str(project['id']), 'folder': '', 'path': 'docs/secret-plan.md'}}
    assert 'secret-plan' not in str(saved.json()['layout'])
    with api.app.state.db() as db:
        assert 'secret-plan' not in str(db.scalar(select(PersonalLayout.layout)))


async def test_unknown_and_revoked_references_are_unavailable_without_a_path(personal):
    api, project, root, url = personal
    reference = await mint(api, root, project)
    await api.put(url, json={'expected_revision': 0, 'layout': file_layout(reference, {'id': 'gone', 'kind': 'file', 'file_ref': 'unknown-reference'})})
    assert (await api.get(url)).json()['resources']['gone'] == {'available': False}
    with api.app.state.db() as db:
        db.get(Project, project['id']).archived = True
        db.commit()
    record = await api.get(url)
    assert record.json()['resources']['f-' + reference] == {'available': False}
    assert 'secret-plan' not in record.text


async def test_one_persons_reference_means_nothing_to_another(personal, member_accounts):
    api, project, root, url = personal
    reference = await mint(api, root, project)
    invite = await api.post('/api/users/invites', json={'account': 'dana-agent'}, headers=csrf(api))
    async with browser(api) as member:
        await join(member, invite.json()['link'], 'dana')
        member.headers.update(csrf(member))
        assert (await member.post(root + '/file-refs', json={'location': 'project', 'id': str(project['id']), 'path': 'a.md'})).status_code == 404
        made = await member.post('/api/projects/new', json={'name': 'Theirs'})
        own = f'/api/projects/{made.json()["id"]}/layouts'
        stored = await member.put(own + '/desktop/wide', json={'expected_revision': 0, 'layout': file_layout(reference)})
        assert stored.status_code == 200, stored.text
        assert stored.json()['resources']['f-' + reference] == {'available': False}
        assert 'secret-plan' not in stored.text
        mine = await mint(member, own, made.json())
    # The owner may reach this project, but a member's reference still resolves for them alone.
    borrowed = await api.put(f'/api/projects/{made.json()["id"]}/layouts/desktop/wide', json={'expected_revision': 0, 'layout': file_layout(mine)})
    assert borrowed.json()['resources']['f-' + mine] == {'available': False}


@pytest.mark.parametrize('body', [
    {'location': 'project', 'path': 'a.md'}, {'location': 'file', 'id': '1', 'path': 'a.md'},
    {'location': 'project', 'id': '1', 'path': '/etc/passwd'}, {'location': 'project', 'id': '1', 'path': '../up'},
    {'location': 'project', 'id': '1', 'path': 'a/./b'}, {'location': 'project', 'id': '1', 'path': 'a\\b'},
    {'location': 'project', 'id': '1', 'path': 'a\nb'}, {'location': 'project', 'id': '1', 'path': ''},
    {'location': 'project', 'id': '1', 'path': 'x' * 2049}, {'location': 'project', 'id': '1', 'path': 7},
    {'location': 'project', 'id': '999', 'path': 'a.md'},
])
async def test_reference_requests_are_bounded(personal, body):
    api, project, root, _ = personal
    body = {**body, **({'id': str(project['id'])} if body.get('id') == '1' else {})}
    assert (await api.post(root + '/file-refs', json=body)).status_code == 422


async def test_a_session_reference_must_belong_to_the_project(personal):
    api, project, root, _ = personal
    assert (await api.post(root + '/file-refs', json={'location': 'session', 'id': 'nothing', 'path': 'a.md'})).status_code == 404
    with api.app.state.db() as db:
        other = Project(slug='other', name='Other', workspace_path='/other', account='agent', owner=db.get(Project, project['id']).owner)
        db.add(other)
        db.flush()
        db.add(Session(id='elsewhere', project_id=other.id, provider='claude', name='x', owner=other.owner, run_as='agent', tmux_name='acc-elsewhere',
                       launch_spec=LaunchSpec(provider='claude', inputs={}, files={}, digest='fixture')))
        db.commit()
    assert (await api.post(root + '/file-refs', json={'location': 'session', 'id': 'elsewhere', 'path': 'a.md'})).status_code == 404


async def test_writes_need_csrf_and_a_person(personal):
    api, project, root, url = personal
    token = api.headers.pop('X-ACC-CSRF')
    try:
        for reply in (await api.put(url, json={'expected_revision': 0, 'layout': file_layout('x')}),
                      await api.post(url + '/reset', json={'expected_revision': 0}),
                      await api.post(root + '/file-refs', json={'location': 'project', 'id': str(project['id']), 'path': 'a.md'})):
            assert reply.status_code == 403
    finally:
        api.headers['X-ACC-CSRF'] = token
    with api.app.state.db() as db:
        assert not db.scalars(select(LayoutFileRef)).all()


async def test_version_one_rows_read_as_one_column_and_save_as_version_two(personal):
    api, project, _, url = personal
    with api.app.state.db() as db:
        owner = db.scalar(select(User.username))
        db.add(PersonalLayout(owner=owner, project_id=project['id'], device='desktop', viewport='wide', revision=4, layout={
            'schema_version': 1, 'selected_tab': 'two', 'sidebar': {'collapsed': True}, 'views': [
                {'id': 'one', 'resource': {'kind': 'session', 'id': 'missing'}, 'geometry': {'x': 1}},
                {'id': 'two', 'resource': {'kind': 'project', 'id': project['id']}}, {'id': 'bare'}]}))
        db.commit()
    record = (await api.get(url)).json()
    assert record['schema_version'] == 2 and record['revision'] == 4
    layout = record['layout']
    assert layout == {'schema_version': 2, 'focused': 0, 'filled': None, 'sidebar': {'collapsed': True},
                      'columns': [{'tabs': ['one', 'two', 'bare'], 'active': 'two', 'width': 1}],
                      'views': [{'id': 'one', 'kind': 'terminal', 'resource': {'kind': 'session', 'id': 'missing'}},
                                {'id': 'two', 'kind': 'page', 'resource': {'kind': 'project', 'id': project['id']}},
                                {'id': 'bare', 'kind': 'page'}]}
    assert record['resources'] == {'one': {'available': False}, 'two': {'available': True}, 'bare': {'available': False}}
    with api.app.state.db() as db:  # reading changes nothing
        assert db.scalar(select(PersonalLayout.layout))['schema_version'] == 1
    saved = await api.put(url, json={'expected_revision': 4, 'layout': layout})
    assert saved.status_code == 200 and saved.json()['revision'] == 5
    with api.app.state.db() as db:
        assert db.scalar(select(PersonalLayout.layout))['schema_version'] == 2
    assert layout_store.migrate(None) is None


async def test_a_stale_write_sees_the_newer_layout_and_changes_nothing(personal):
    api, project, root, url = personal
    first = file_layout(await mint(api, root, project, 'first.md'))
    second = file_layout(await mint(api, root, project, 'second.md'))
    await api.put(url, json={'expected_revision': 0, 'layout': first})
    await api.put(url, json={'expected_revision': 1, 'layout': second})
    stale = await api.put(url, json={'expected_revision': 1, 'layout': first})
    assert stale.status_code == 409
    assert stale.json()['current']['layout'] == second and stale.json()['current']['revision'] == 2


async def test_references_are_bounded_and_keep_those_in_use(personal, monkeypatch):
    api, project, root, url = personal
    monkeypatch.setattr(layout_store, 'MAX_FILE_REFS', 2)
    used = await mint(api, root, project, 'used.md')
    await api.put(url, json={'expected_revision': 0, 'layout': file_layout(used)})
    await mint(api, root, project, 'spare.md')
    await mint(api, root, project, 'newest.md')
    with api.app.state.db() as db:
        assert sorted(db.scalars(select(LayoutFileRef.path))) == ['newest.md', 'used.md']
    await api.put(url, json={'expected_revision': 1, 'layout': file_layout(used, {'id': 'b', 'kind': 'file', 'file_ref': (await mint(api, root, project, 'newest.md'))})})
    assert (await api.post(root + '/file-refs', json={'location': 'project', 'id': str(project['id']), 'path': 'full.md'})).status_code == 409


async def test_project_deletion_removes_references(personal):
    api, project, root, _ = personal
    await mint(api, root, project)
    with api.app.state.db() as db:
        db.delete(db.get(Project, project['id']))
        db.commit()
        assert not db.scalars(select(LayoutFileRef)).all()


def refs(api):
    with api.app.state.db() as db:
        return sorted(db.scalars(select(LayoutFileRef.path)))


async def test_a_path_is_kept_only_while_a_saved_layout_uses_it(personal):
    api, project, root, url = personal
    first, second = await mint(api, root, project, 'first.md'), await mint(api, root, project, 'second.md')
    await api.put(url, json={'expected_revision': 0, 'layout': file_layout(first, {'id': 'b', 'kind': 'file', 'file_ref': second})})
    assert refs(api) == ['first.md', 'second.md']
    await api.put(url, json={'expected_revision': 1, 'layout': file_layout(first)})  # closing a view is a save
    assert refs(api) == ['first.md']


async def test_a_refused_save_prunes_nothing(personal):
    api, project, root, url = personal
    first = await mint(api, root, project, 'first.md')
    await api.put(url, json={'expected_revision': 0, 'layout': file_layout(first)})
    other = {**file_layout('x'), 'views': [{'id': 'f-x', 'kind': 'page'}]}
    assert (await api.put(url, json={'expected_revision': 0, 'layout': other})).status_code == 409
    assert refs(api) == ['first.md']


async def test_another_device_class_keeps_its_reference(personal):
    api, project, root, url = personal
    kept, dropped = await mint(api, root, project, 'kept.md'), await mint(api, root, project, 'dropped.md')
    await api.put(url.replace('desktop', 'phone'), json={'expected_revision': 0, 'layout': file_layout(kept)})
    await api.put(url, json={'expected_revision': 0, 'layout': file_layout(dropped)})
    await api.put(url, json={'expected_revision': 1, 'layout': file_layout(kept)})
    assert refs(api) == ['kept.md']


async def test_reset_deletes_this_persons_references_for_the_project(personal, member_accounts):
    api, project, root, url = personal
    first = await mint(api, root, project, 'first.md')
    await api.put(url, json={'expected_revision': 0, 'layout': file_layout(first)})
    await api.put(url.replace('desktop', 'phone'), json={'expected_revision': 0, 'layout': file_layout(first)})
    with api.app.state.db() as db:
        owner = db.scalar(select(LayoutFileRef.owner))
        other = Project(slug='elsewhere', name='Elsewhere', workspace_path='/elsewhere', account='agent', owner=db.get(Project, project['id']).owner)
        db.add(other)
        db.flush()
        db.add(LayoutFileRef(id='other-project', owner=owner, project_id=other.id, location='project', target=str(other.id), path='keep.md', path_hash='k' * 64))
        db.commit()
    assert (await api.post(url + '/reset', json={'expected_revision': 0})).status_code == 409  # stale: nothing goes
    assert refs(api) == ['first.md', 'keep.md']
    assert (await api.post(url + '/reset', json={'expected_revision': 1})).status_code == 200
    assert refs(api) == ['keep.md']


@pytest.mark.parametrize('field,value', [
    ('path', 'a\ud800b'), ('folder', '\ud800'), ('folder', 'a\x00b'), ('folder', 'a\nb'), ('folder', 'a/b'), ('folder', '..'),
])
async def test_reference_text_must_be_storable(personal, field, value):
    import json
    api, project, root, _ = personal
    body = {'location': 'project', 'id': str(project['id']), 'path': 'a.md', field: value}
    response = await api.post(root + '/file-refs', content=json.dumps(body), headers={'content-type': 'application/json'})
    assert response.status_code == 422, response.text
