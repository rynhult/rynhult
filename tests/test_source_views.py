"""P4.3 reads preserve metadata audiences and live placement authority."""
# ruff: noqa: F811
import pytest
from sqlalchemy import select
from test_groups import grouped, two  # noqa: F401
from test_sources import legacy

from acc.server.models import Placement, Project
from acc.server.source_models import ProjectRepository, RepositoryLocator


async def test_single_source_view_and_local_only_directory(api):
    with api.app.state.db() as db:
        p = legacy(db, remote_url='https://example.com/alex/demo.git')
        local = legacy(db, slug='local')
        db.commit()
        ident, local_id = p.id, local.id
    response = await api.get(f'/api/source-views/projects/{ident}')
    assert response.status_code == 200, response.text
    view = response.json()
    assert view['counts'] == {'repositories': 1, 'locations': 1}
    assert view['repositories'][0]['verification'] == 'unresolved'
    assert view['repositories'][0]['owner'] is None
    assert view['locations'][0]['compatibility_default'] is True
    result = (await api.get('/api/source-views/directory?limit=1')).json()
    assert result['total'] == 2 and len(result['items']) == 1
    assert result['next_offset'] == 1
    local_view = (await api.get(f'/api/source-views/projects/{local_id}')).json()
    assert local_view['counts'] == {'repositories': 0, 'locations': 1}
    assert (await api.get('/api/source-views/directory?limit=101')).status_code == 422
    assert (await api.get('/api/source-views/projects/999999')).status_code == 404


async def test_ac038_registration_counts_preserve_host_account_alias_identity(api):
    with api.app.state.db() as db:
        p = legacy(db, remote_url='https://example.com/alex/demo.git')
        for host, account, path in [('edge', 'alex-agent', '/work/demo'),
                                    ('edge', 'casey-agent', '/work/demo'),
                                    ('local', 'alex-agent', '/work/alias')]:
            db.add(Placement(project_id=p.id, host=host, account=account, path=path, person='alex'))
        db.commit()
        ident = p.id
    view = (await api.get(f'/api/source-views/projects/{ident}')).json()
    assert view['counts'] == {'repositories': 1, 'locations': 4}
    assert len({r['location_id'] for r in view['locations']}) == 4
    assert len({r['registration_id'] for r in view['locations']}) == 4
    assert sum(r['compatibility_default'] for r in view['locations']) == 1
    # The view never rebinds or coalesces a registration by path spelling.
    from acc.server.sources import inventory
    with api.app.state.db() as db:
        assert len(inventory(db, db.get(Project, ident))['locations']) == 4


@pytest.mark.parametrize('role', ['member', 'maintainer', 'viewer', 'removed'])
async def test_group_snapshot_current_person_authority_and_revocation(grouped, role):
    from helpers import write_db

    from acc.server.models import GroupMembership
    w = grouped
    ident = w['group_project']['id']
    with write_db(w['owner'].app.state.db) as db:
        for person, path in [('alex', '/alex'), ('casey', '/secret-casey'), (None, '/secret-unassigned')]:
            db.add(Placement(project_id=ident, host='local', account='agent', path=path, person=person))
        held = db.get(GroupMembership, (w['group']['id'], 2))
        if role == 'removed':
            db.delete(held)
        else:
            held.role = role
        db.commit()
    url = f"/api/source-views/groups/{w['group']['id']}/projects/{ident}"
    response = await w['member'].get(url)
    assert response.status_code == (404 if role == 'removed' else 200), response.text
    if role != 'removed':
        body = response.json()
        assert [x['path'] for x in body['locations']] == ([] if role == 'viewer' else ['/alex'])
        assert 'secret-' not in response.text
        assert body['counts']['locations'] == (0 if role == 'viewer' else 1)
    assert (await w['member'].get(f'/api/source-views/projects/{ident}')).status_code == 404


async def test_metadata_domain_is_not_implied_by_repository_existence(api):
    with api.app.state.db() as db:
        p = legacy(db, remote_url='https://example.com/private/hidden.git')
        db.commit()
        membership = db.scalar(select(ProjectRepository).where(ProjectRepository.project_id == p.id))
        membership.visibility_domain = 'person:other'
        db.commit()
        ident = p.id
    view = (await api.get(f'/api/source-views/projects/{ident}')).json()
    assert view['repositories'] == []
    # No source-linked path or count may bypass a hidden membership.
    assert view['locations'] == []
    assert view['counts'] == {'repositories': 0, 'locations': 0}
    directory = (await api.get('/api/source-views/directory')).json()
    assert 'hidden.git' not in str(directory)


async def test_member_directory_and_cross_person_locator_are_private(two):
    w = two
    with w['owner'].app.state.db() as db:
        own = legacy(db, slug='owned', remote_url='https://example.com/alex/owned.git')
        other = Project(slug='private-other', name='Private other', owner='casey', account='casey-agent',
                        workspace_path='/secret', remote_url='https://example.com/private/secret.git')
        db.add(other)
        db.commit()
        membership = db.scalar(select(ProjectRepository).where(ProjectRepository.project_id == own.id))
        locator = db.get(RepositoryLocator, membership.locator_id)
        locator.authority = 'person:casey'
        db.commit()
        own_id, other_id = own.id, other.id
    response = await w['member'].get('/api/source-views/directory')
    assert response.status_code == 200
    assert 'secret' not in response.text
    assert 'owned.git' not in response.text
    assert (await w['member'].get(f'/api/source-views/projects/{other_id}')).status_code == 404
    assert (await w['member'].get(f'/api/source-views/projects/{own_id}')).json()['repositories'] == []
    assert (await w['member'].get(f'/api/source-views/projects/{own_id}')).json()['audience_mismatch'] is False


async def test_directory_filters_match_the_same_edge_and_no_location_source(api):
    from acc.server.source_models import HostingInstance, ProjectLocation, Repository, SourceOwner
    with api.app.state.db() as db:
        p = legacy(db, remote_url='https://example.com/alex/first.git')
        db.add(Placement(project_id=p.id, host='edge', account='alex-agent', path='/edge', person='alex'))
        db.commit()
        first = db.scalar(select(ProjectRepository).where(ProjectRepository.project_id == p.id))
        db.add(HostingInstance(id='instance', provider='github', api_origin='https://api.github.com'))
        db.flush()
        for key in ('a', 'b', 'no-location'):
            db.add(SourceOwner(id=key, instance_id='instance', provider_owner_id=key, login=key))
            db.flush()
            db.add(Repository(id='repo-' + key, instance_id='instance', provider_repository_id=key, owner_id=key, name='same-name', revision=1))
        db.flush()
        first.locator_id, first.repository_id = None, 'repo-a'
        for key in ('b', 'no-location'):
            db.add(ProjectRepository(id=key, project_id=p.id, legacy_project_id=p.id, repository_id='repo-' + key,
                                    visibility_domain='person:alex', compatibility_primary=False, revision=1))
        db.flush()
        links = list(db.scalars(select(ProjectLocation).where(ProjectLocation.project_id == p.id)))
        from acc.server.source_models import WorkingLocation
        edge = next(link for link in links if db.get(WorkingLocation, link.location_id).recorded_path == '/edge')
        edge.membership_id = 'b'
        host = db.get(WorkingLocation, edge.location_id).host_id
        db.commit()
    assert (await api.get(f'/api/source-views/directory?source_owner=a&host_id={host}')).json()['total'] == 0
    result = (await api.get(f'/api/source-views/directory?source_owner=b&host_id={host}')).json()
    assert result['total'] == 1
    assert result['items'][0]['counts'] == {'repositories': 3, 'locations': 2}
    assert (await api.get('/api/source-views/directory?source_owner=no-location')).json()['total'] == 1
    assert (await api.get(f'/api/source-views/directory?source_owner=no-location&host_id={host}')).json()['total'] == 0
    assert (await api.get('/api/source-views/directory?host_id=replaced-host')).json()['total'] == 0
    assert (await api.get('/api/source-views/directory?q=%25')).json()['total'] == 0


async def test_private_placement_cannot_qualify_directory_filter(two):
    from acc.server.source_models import WorkingLocation
    with two['owner'].app.state.db() as db:
        p = legacy(db, slug='member-location')
        db.add(Placement(project_id=p.id, person='casey', host='private-host', account='casey-agent', path='/private'))
        db.commit()
        host = db.scalar(select(WorkingLocation.host_id).where(WorkingLocation.recorded_path == '/private'))
    assert (await two['member'].get(f'/api/source-views/directory?host_id={host}')).json()['total'] == 0
    assert (await two['owner'].get(f'/api/source-views/directory?host_id={host}')).json()['total'] == 1


async def test_large_project_is_bounded_without_silent_count_loss(api):
    with api.app.state.db() as db:
        p = legacy(db)
        for n in range(105):
            db.add(Placement(project_id=p.id, person='alex', host='local', account='alex-agent', path=f'/copy-{n}'))
        db.commit()
        ident = p.id
    response = await api.get(f'/api/source-views/projects/{ident}')
    body = response.json()
    assert len(body['locations']) == 100
    assert body['counts']['locations'] == 106
    assert body['truncated']['locations'] is True
    assert response.headers['cache-control'] == 'private, no-store'


async def test_shared_routing_identity_keeps_each_custody_registration(api):
    from acc.server.source_models import LocationRegistration, ProjectLocation
    with api.app.state.db() as db:
        p = legacy(db)
        db.commit()
        original = db.scalar(select(ProjectLocation).where(ProjectLocation.project_id == p.id))
        db.add(LocationRegistration(id='shared-registration', project_id=p.id, legacy_project_id=p.id,
                                   location_id=original.location_id, registration_key='shared-reference', provenance={}))
        db.flush()
        db.add(ProjectLocation(id='shared-edge', project_id=p.id, location_id=original.location_id,
                               registration_id='shared-registration', trust='pending', sync_status='unknown'))
        db.commit()
        ident = p.id
    view = (await api.get(f'/api/source-views/projects/{ident}')).json()
    assert view['counts']['locations'] == 1
    assert view['registration_count'] == 2
    assert len(view['locations']) == 2
    assert len({row['registration_id'] for row in view['locations']}) == 2


async def test_repeated_membership_of_verified_repository_is_one_source(api):
    from acc.server.source_models import HostingInstance, Repository
    with api.app.state.db() as db:
        p = legacy(db)
        db.add(HostingInstance(id='instance', provider='github', api_origin='https://api.github.com'))
        db.flush()
        db.add(Repository(id='repo', instance_id='instance', provider_repository_id='42', name='cedar', revision=1))
        db.flush()
        for key in ('one', 'two'):
            db.add(ProjectRepository(id=key, project_id=p.id, legacy_project_id=p.id, repository_id='repo',
                                    visibility_domain='person:alex', compatibility_primary=False, revision=1))
        db.commit()
        ident = p.id
    view = (await api.get(f'/api/source-views/projects/{ident}')).json()
    assert view['counts']['repositories'] == 1
    assert view['membership_count'] == 2
    assert len(view['repositories']) == 1
    assert set(view['repositories'][0]['membership_ids']) == {'one', 'two'}


def test_projection_reads_source_metadata_in_bounded_queries(api):
    from sqlalchemy import event

    from acc.server.source_models import HostingInstance, Repository
    from acc.server.source_views import snapshot
    with api.app.state.db() as db:
        p = legacy(db)
        db.add(HostingInstance(id='instance', provider='github', api_origin='https://api.github.com'))
        db.flush()
        for n in range(20):
            db.add(Repository(id=f'repo-{n}', instance_id='instance', provider_repository_id=str(n), name=f'app-{n}', revision=1))
        db.flush()
        for n in range(20):
            db.add(ProjectRepository(id=f'membership-{n}', project_id=p.id, legacy_project_id=p.id, repository_id=f'repo-{n}',
                                    visibility_domain='person:alex', compatibility_primary=False, revision=1))
        db.commit()
        ident = p.id
        db.expunge_all()
        p = db.get(Project, ident)
        queries = []
        def record(conn, cursor, statement, parameters, context, many):
            queries.append(statement)
        event.listen(db.bind, 'before_cursor_execute', record)
        try:
            view = snapshot(db, p)
        finally:
            event.remove(db.bind, 'before_cursor_execute', record)
        assert view['counts']['repositories'] == 20
        assert len(queries) <= 12, f'source read performed {len(queries)} queries for 20 repositories'


async def test_directory_project_page_scope_is_filtered_before_pagination(api):
    with api.app.state.db() as db:
        regular = legacy(db, slug='regular')
        legacy(db, slug='workspace', private_workspace=True)
        legacy(db, slug='local-alex')
        legacy(db, slug='archived', archived=True)
        db.commit()
        ident = regular.id
    page = (await api.get('/api/source-views/directory?projects_only=true&include_archived=false&limit=1')).json()
    assert page['total'] == 1
    assert page['next_offset'] is None
    assert page['items'][0]['id'] == ident


async def test_directory_searches_only_authorized_source_text_and_project_notes(api):
    with api.app.state.db() as db:
        p = legacy(db, remote_url='https://example.com/cedar/woodland-api.git')
        p.notes = 'Restore workshop notes'
        db.commit()
        ident = p.id
    assert (await api.get('/api/source-views/directory?q=woodland-api')).json()['total'] == 1
    assert (await api.get('/api/source-views/directory?q=workshop')).json()['total'] == 1
    with api.app.state.db() as db:
        membership = db.scalar(select(ProjectRepository).where(ProjectRepository.project_id == ident))
        membership.visibility_domain = 'person:other'
        db.commit()
    assert (await api.get('/api/source-views/directory?q=woodland-api')).json()['total'] == 0


async def test_v1_machine_checkin_remains_owner_only_on_all_view_routes(grouped):
    from datetime import UTC, datetime

    from helpers import write_db

    from acc.server.models import Node
    w = grouped
    checked = datetime(2026, 10, 30, 12, tzinfo=UTC)
    with write_db(w['owner'].app.state.db) as db:
        db.add(Node(id='checkin-node', name='edge', status='active', last_seen=checked))
        db.flush()
        p = Project(slug='member-edge', name='Member edge', owner='alex', host='edge',
                    account='alex-agent', workspace_path='/work/member')
        db.add(p)
        db.flush()
        ident = p.id
        db.add(Placement(project_id=w['group_project']['id'], host='edge', account='alex-agent',
                         path='/work/group-member', person='alex'))
        db.commit()
    assert (await w['member'].get('/api/host?machine=edge')).status_code == 403
    assert (await w['member'].get('/api/nodes')).status_code == 403
    assert checked.isoformat() not in (await w['member'].get('/api/projects')).text
    paths = [f'/api/source-views/projects/{ident}', '/api/source-views/directory',
             f"/api/source-views/groups/{w['group']['id']}/projects/{w['group_project']['id']}"]
    for path in paths:
        member = await w['member'].get(path)
        owner = await w['owner'].get(path)
        assert member.status_code == owner.status_code == 200
        member_views = member.json()['items'] if path.endswith('directory') else [member.json()]
        owner_views = owner.json()['items'] if path.endswith('directory') else [owner.json()]
        member_locations = [row for view in member_views for row in view['locations'] if row['host'] == 'edge']
        owner_locations = [row for view in owner_views for row in view['locations'] if row['host'] == 'edge']
        assert member_locations and owner_locations
        assert all(row['host_status'] == 'not_checked' for row in member_locations)
        assert all(row['last_seen_at'] is None for row in member_locations)
        assert checked.isoformat() not in member.text
        assert all(row['last_seen_at'] == checked.isoformat() for row in owner_locations)


@pytest.mark.parametrize('role', ['member', 'maintainer', 'viewer'])
async def test_v2_verified_group_metadata_follows_locator_redaction(grouped, role):
    from helpers import write_db

    from acc.server.models import GroupMembership
    from acc.server.source_models import HostingInstance, Repository, SourceOwner
    w = grouped
    ident = w['group_project']['id']
    with write_db(w['owner'].app.state.db) as db:
        db.get(GroupMembership, (w['group']['id'], 2)).role = role
        db.add(HostingInstance(id='private-instance', provider='github', api_origin='https://git.example.com/api'))
        db.flush()
        db.add(SourceOwner(id='private-owner', instance_id='private-instance', provider_owner_id='42', login='cedar-private-org'))
        db.flush()
        db.add(Repository(id='private-repo', instance_id='private-instance', provider_repository_id='99',
                          owner_id='private-owner', name='secret-repo', revision=1))
        db.flush()
        db.add(ProjectRepository(id='private-membership', project_id=ident, legacy_project_id=ident, repository_id='private-repo',
                                 visibility_domain='group:' + w['group']['id'], revision=1))
        db.commit()
    url = f"/api/source-views/groups/{w['group']['id']}/projects/{ident}"
    response = await w['member'].get(url)
    assert response.status_code == 200
    row = next(row for row in response.json()['repositories'] if row['repository_id'] == 'private-repo')
    assert row['locator'] is None
    assert row['name'] is None and row['owner'] is None and row['instance'] is None
    assert all(secret not in response.text for secret in ('secret-repo', 'cedar-private-org', 'https://git.example.com/api'))
    owner = await w['owner'].get(url)
    assert owner.status_code == 200
    assert 'secret-repo' in owner.text and 'cedar-private-org' in owner.text


async def test_v3_transferred_project_reports_audience_mismatch_without_rebinding(two):
    with two['owner'].app.state.db() as db:
        p = legacy(db, slug='transfer-source', remote_url='https://example.com/previous/private.git')
        db.commit()
        ident = p.id
    removed = await two['owner'].post('/api/users/2/remove', json={'resources': 'transfer'})
    assert removed.status_code == 200, removed.text
    response = await two['owner'].get(f'/api/source-views/projects/{ident}')
    assert response.status_code == 200
    assert response.json()['audience_mismatch'] is True
    assert response.json()['counts'] == {'repositories': 0, 'locations': 0}
    assert 'private.git' not in response.text
    directory = (await two['owner'].get('/api/source-views/directory')).json()
    assert next(row for row in directory['items'] if row['id'] == ident)['audience_mismatch'] is True
    with two['owner'].app.state.db() as db:
        assert db.get(Project, ident).owner == '@api'  # owner fixture uses the installation API principal
        assert db.scalar(select(ProjectRepository.visibility_domain).where(ProjectRepository.project_id == ident)) == 'person:alex'


async def test_directory_sorts_before_pagination_and_validates_keys(api):
    with api.app.state.db() as db:
        first = legacy(db, slug='sort-z', trusted=False, compose_autostart=True)
        second = legacy(db, slug='sort-a', trusted=True, compose_autostart=False)
        first.name = 'Alpha'; second.name = 'Zulu'
        db.commit()
        first_id, second_id = first.id, second.id
    for key, expected in [('name', first_id), ('status', first_id), ('runtime', second_id)]:
        ascending = (await api.get(f'/api/source-views/directory?sort={key}&limit=1')).json()
        assert ascending['items'][0]['id'] == expected
        descending = (await api.get(f'/api/source-views/directory?sort={key}&direction=desc&limit=1')).json()
        assert descending['items'][0]['id'] != expected
        assert ascending['next_offset'] == 1
    row = (await api.get('/api/source-views/directory?sort=activity&limit=1')).json()['items'][0]
    assert row['last_activity'] is None
    assert row['runtime'] in ('Autostart on', 'Autostart off')
    assert (await api.get('/api/source-views/directory?sort=sql')).status_code == 422
    assert (await api.get('/api/source-views/directory?direction=sideways')).status_code == 422


async def test_directory_activity_uses_latest_timestamp_and_viewer_scope(api):
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from acc.server.models import LaunchSpec, Session
    from acc.server.source_views import directory

    old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    recent = datetime(2026, 2, 1, tzinfo=timezone.utc)
    private = datetime(2026, 3, 1, tzinfo=timezone.utc)
    with api.app.state.db() as db:
        p = legacy(db, slug='activity-one')
        other = legacy(db, slug='activity-two')
        for ident, project, owner, status_at, activity_at, output_at in [
            ('activity-one', p, 'alex', old, old, recent),
            ('activity-two', other, 'alex', old, old, None),
            ('activity-private', other, 'casey', private, private, private),
        ]:
            db.add(Session(id=ident, name=ident, provider='claude', project=project, owner=owner,
                           run_as='alex-agent', tmux_name=ident, status_at=status_at,
                           activity_at=activity_at, last_output_at=output_at,
                           launch_spec=LaunchSpec(provider='claude', inputs={}, files={}, digest=ident)))
        db.flush()
        member = directory(db, SimpleNamespace(is_owner=False, username='alex'), sort='activity', direction='desc', limit=1)
        assert member['items'][0]['id'] == p.id
        assert member['items'][0]['last_activity'] == recent
        owner = directory(db, SimpleNamespace(is_owner=True), sort='activity', direction='desc', limit=1)
        assert owner['items'][0]['id'] == other.id
        assert owner['items'][0]['last_activity'] == private


@pytest.mark.parametrize('sort', ['status', 'runtime'])
@pytest.mark.parametrize('direction', ['asc', 'desc'])
async def test_directory_sort_ties_use_name_then_id_before_pagination(api, sort, direction):
    with api.app.state.db() as db:
        zulu = legacy(db, slug='ties-first')
        alpha = legacy(db, slug='ties-second')
        same = legacy(db, slug='ties-third')
        zulu.name = 'Zulu'; alpha.name = 'alpha'; same.name = 'Alpha'
        db.commit()
        expected = [alpha.id, same.id, zulu.id]
    actual = []
    for offset in range(3):
        response = await api.get(f'/api/source-views/directory?sort={sort}&direction={direction}&limit=1&offset={offset}')
        assert response.status_code == 200
        actual.append(response.json()['items'][0]['id'])
    assert actual == expected
