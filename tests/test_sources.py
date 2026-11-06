"""P4.1 compatibility identities never widen authority or guess physical identity."""
# ruff: noqa: F811
import pytest
from alembic import command
from sqlalchemy import select
from sqlalchemy.orm import Session

from acc.server.db import alembic_config, make_engine
from acc.server.models import Placement, Project


def legacy(db, slug='demo', **values):
    row = Project(slug=slug, name=slug, host='local', account='alex-agent',
                  workspace_path='/work/demo', owner='alex', **values)
    db.add(row)
    db.flush()
    return row


def test_upgrade_preserves_registration_and_restart(tmp_path):
    from acc.server.sources import inventory
    url = 'sqlite:///' + str(tmp_path / 'old.db')
    cfg = alembic_config(url)
    command.upgrade(cfg, '0081')
    engine = make_engine(url)
    with engine.begin() as db:
        # Core inserts deliberately simulate old binaries, without ORM compatibility hooks.
        for ident, slug, remote, private, mode, managed in [
            (1, 'local', None, False, 'managed', False),
            (2, 'remote', 'https://example.com/alex/demo.git', False, 'managed', True),
            (3, 'private', None, True, 'managed', False),
            (4, 'imported', None, False, 'readonly', False),
        ]:
            db.execute(Project.__table__.insert().values(id=ident, slug=slug, name=slug,
                host='local', account='alex-agent', workspace_path='/work/demo', owner='alex',
                remote_url=remote, private_workspace=private, import_mode=mode, managed_checkout=managed))
        db.execute(Placement.__table__.insert().values(id=12, project_id=2, person='alex',
            host='edge', account='alex-agent', path='/work/demo', trust='approved', sync_status='ready'))
    command.upgrade(cfg, 'head')
    with Session(engine) as db:
        local = inventory(db, db.get(Project, 1))
        remote = inventory(db, db.get(Project, 2))
        assert local['repositories'] == []
        assert len(remote['repositories']) == 1
        assert remote['repositories'][0]['verification'] == 'unresolved'
        assert len(remote['locations']) == 2
        assert len({p['location_id'] for p in remote['locations']}) == 2
        imported = inventory(db, db.get(Project, 4))['locations'][0]
        assert imported['provenance']['managed_checkout'] is False
        assert imported['provenance']['import_mode'] == 'readonly'
        assert db.get(Project, 3).private_workspace is True
        assert db.get(Placement, 12).path == '/work/demo'
        before = remote
    command.downgrade(cfg, '0081')
    command.upgrade(cfg, 'head')
    with Session(engine) as db:
        assert inventory(db, db.get(Project, 2)) == before
    engine.dispose()


def test_new_projects_and_placements_have_explicit_compatibility_target(api):
    from acc.server.sources import compatibility_target, inventory
    with api.app.state.db() as db:
        project = legacy(db, remote_url='https://example.com/alex/demo.git')
        db.commit()
        assert compatibility_target(db, project)['path'] == '/work/demo'
        db.add(Placement(project_id=project.id, host='edge', account='alex-agent', path='/work/demo', person='alex'))
        db.commit()
        rows = inventory(db, project)['locations']
        assert len(rows) == 2
        assert rows[0]['location_id'] != rows[1]['location_id']
        db.add(Placement(project_id=project.id, host='local', account='casey-agent', path='/work/demo', person='casey'))
        db.commit()
        rows = inventory(db, project)['locations']
        assert len({row['location_id'] for row in rows}) == 3
        assert len({row['account_id'] for row in rows}) == 3


def test_launch_refuses_stale_or_extra_membership(api):
    from acc.server.source_models import ProjectRepository
    from acc.server.sources import Refused, compatibility_target
    with api.app.state.db() as db:
        project = legacy(db, remote_url='https://example.com/alex/demo.git')
        db.commit()
        member = db.scalar(select(ProjectRepository).where(ProjectRepository.project_id == project.id))
        db.add(ProjectRepository(id='extra', project_id=project.id, legacy_project_id=project.id,
                                locator_id=member.locator_id, visibility_domain='person:alex', compatibility_primary=False, revision=1))
        db.commit()
        with pytest.raises(Refused, match='multiple'):
            compatibility_target(db, project)
        db.delete(db.get(ProjectRepository, 'extra'))
        db.commit()
        project.workspace_path = '/changed'
        db.commit()
        with pytest.raises(Refused, match='changed'):
            compatibility_target(db, project)


async def test_personal_read_uses_existing_project_authority(api):
    with api.app.state.db() as db:
        project = legacy(db)
        db.commit()
        ident = project.id
    for suffix in ('repositories', 'locations'):
        result = await api.get(f'/api/projects/{ident}/{suffix}')
        assert result.status_code == 200, result.text
        assert isinstance(result.json(), list)
        assert (await api.get(f'/api/projects/999999/{suffix}')).status_code == 404


def test_downgrade_refuses_new_only_state(tmp_path):
    from acc.server.source_models import HostingInstance
    url = 'sqlite:///' + str(tmp_path / 'new.db')
    cfg = alembic_config(url)
    command.upgrade(cfg, 'head')
    engine = make_engine(url)
    with Session(engine) as db:
        db.add(HostingInstance(id='github', provider='github', api_origin='https://api.github.com'))
        db.commit()
    with pytest.raises(RuntimeError, match='restore'):
        command.downgrade(cfg, '0081')
    engine.dispose()


def test_scoped_locators_and_verified_ids_do_not_merge_by_name(api):
    from sqlalchemy.exc import IntegrityError

    from acc.server.source_models import HostingInstance, ProjectRepository, Repository, RepositoryLocator
    with api.app.state.db() as db:
        one = legacy(db, slug='one', remote_url='https://example.com/alex/demo.git')
        two = Project(slug='two', name='two', host='local', account='other-agent',
            workspace_path='/work/demo', owner='casey', remote_url=one.remote_url)
        db.add(two)
        db.commit()
        links = list(db.scalars(select(ProjectRepository)))
        assert len({r.locator_id for r in links}) == 2
        assert len({db.get(RepositoryLocator, r.locator_id).authority for r in links}) == 2
        db.add_all([HostingInstance(id='a', provider='github', api_origin='https://api.github.com'),
                    HostingInstance(id='b', provider='github', api_origin='https://git.example.com/api')])
        db.commit()
        for instance in ('a', 'b'):
            db.add(Repository(id=instance, instance_id=instance, provider_repository_id='42', name='alex/demo'))
        db.commit()
        db.add(Repository(id='dup', instance_id='a', provider_repository_id='42', name='renamed'))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


@pytest.mark.parametrize('suffix', ['repositories', 'locations'])
async def test_personal_read_authorization_matrix(two, suffix):
    from helpers import write_db
    w = two
    with write_db(w['owner'].app.state.db) as db:
        project = Project(slug='member-project', name='member', host='local', account='dana-agent',
            workspace_path='/work/member', owner='alex', remote_url='https://user:secret@example.com/a/b.git?token=secret')
        db.add(project)
        db.commit()
        ident = project.id
    own = await w['member'].get(f'/api/projects/{ident}/{suffix}')
    assert own.status_code == 200, own.text
    assert 'secret' not in own.text
    assert (await w['member'].get(f"/api/projects/{w['project']['id']}/{suffix}")).status_code == 404
    assert (await w['owner'].get(f'/api/projects/{ident}/{suffix}')).status_code == 200


@pytest.mark.parametrize('role', ['maintainer', 'member', 'viewer', 'removed'])
async def test_group_locations_keep_exact_person_filter(grouped, role):
    from helpers import write_db

    from acc.server.models import GroupMembership
    w = grouped
    ident = w['group_project']['id']
    with write_db(w['owner'].app.state.db) as db:
        for person, path in [('alex', '/alex'), ('casey', '/casey'), (None, '/null')]:
            db.add(Placement(project_id=ident, host='local', account='agent', path=path, person=person))
        member = db.get(GroupMembership, (w['group']['id'], 2))
        if role == 'removed':
            db.delete(member)
        else:
            member.role = role
        db.commit()
    base = f"/api/groups/{w['group']['id']}/projects/{ident}"
    result = await w['member'].get(base + '/locations')
    assert result.status_code == (404 if role in ('viewer', 'removed') else 200), result.text
    if result.status_code == 200:
        assert [p['path'] for p in result.json()] == ['/alex']
    assert len((await w['owner'].get(base + '/locations')).json()) == 3
    assert (await w['member'].get(base + '/repositories')).status_code == (404 if role == 'removed' else 200)
    assert (await w['owner'].get(f'/api/projects/{ident}/locations')).status_code == 404


async def test_session_freezes_target_without_changing_launch_spec(two):
    from acc.server.models import Session as AgentSession
    from acc.server.source_models import SessionTarget
    w = two
    with w['owner'].app.state.db() as db:
        session = db.get(AgentSession, w['session']['id'])
        frozen = db.get(SessionTarget, session.id)
        assert frozen is not None
        assert frozen.snapshot['path'] == w['project']['workspace_path']
        assert frozen.snapshot['execution_host'] == session.host
        assert 'membership_id' not in session.launch_spec.inputs


# Reuse the ordinary signed-in project/group fixtures, not bypass principals.
from test_groups import grouped, two  # noqa: E402,F401


def test_upgrade_group_readonly_deleting_and_detached_history(tmp_path):
    import sqlite3

    from acc.server.source_models import LocationRegistration, ProjectLaunchDefault, ProjectLocation, ProjectRepository
    from acc.server.sources import inventory
    url = 'sqlite:///' + str(tmp_path / 'historical.db')
    config = alembic_config(url)
    command.upgrade(config, '0081')
    engine = make_engine(url)
    with engine.begin() as db:
        from acc.server.models import Group
        db.execute(Group.__table__.insert().values(id='team', name='Team', created_by='casey'))
        db.execute(Project.__table__.insert().values(id=50, slug='group', name='Group', group_id='team', owner=None,
            workspace_path='', account='', remote_url='https://example.com/team/demo.git',
            deleting=True, archived=True, import_mode='readonly', managed_checkout=False))
        for ident, person, host in [(70, 'alex', 'edge'), (71, None, 'other')]:
            db.execute(Placement.__table__.insert().values(id=ident, project_id=50, person=person,
                host=host, account='agent', path='/same/Path', trust='pending', sync_status='unprovisioned'))
        from acc.server.models import LaunchSpec
        from acc.server.models import Session as AgentSession
        spec_id = db.execute(LaunchSpec.__table__.insert().values(provider='claude', inputs={'original': True},
            files={}, digest='unchanged').returning(LaunchSpec.id)).scalar_one()
        # Current ORM defaults include columns absent from this historical schema.
        from datetime import datetime

        from sqlalchemy import MetaData, Table
        historical_sessions = Table('session', MetaData(), autoload_with=db)
        db.execute(historical_sessions.insert().values(created_at=datetime.now(), updated_at=datetime.now(),
            multirun_stop_pending=False, team_stop_pending=False, team_depth=0, recovery_attempts=0, spool_offset=0,
            id='detached-history', name='History', provider='claude',
            tmux_name='history', launch_spec_id=spec_id, run_as='alex-agent', project_id=None,
            deleted_project={'slug': 'deleted', 'host': 'edge'}, desired_state='archived', observed_state='archived'))
        # The deleted project's ID must not be resurrected or reused by the backfill.
        db.exec_driver_sql("UPDATE sqlite_sequence SET seq=99 WHERE name='project'")
    engine.dispose()
    backup = tmp_path / 'before.db'
    with sqlite3.connect(tmp_path / 'historical.db') as src, sqlite3.connect(backup) as dst:
        src.backup(dst)
    command.upgrade(config, 'head')
    engine = make_engine(url)
    with Session(engine) as db:
        project = db.get(Project, 50)
        view = inventory(db, project, person='alex', include_origin=False)
        assert len(view['locations']) == 1
        assert view['locations'][0]['provenance']['managed_checkout'] is False
        assert project.deleting and project.archived and project.import_mode == 'readonly'
        assert db.get(ProjectLaunchDefault, 50) is None
        assert len(list(db.scalars(select(ProjectRepository)))) == 1
        assert len(list(db.scalars(select(ProjectLocation)))) == 2
        assert all(r.provenance['group_id'] == 'team' for r in db.scalars(select(LocationRegistration)))
        assert db.get(Project, 99) is None
        from acc.server.source_models import SessionTarget
        history = db.get(AgentSession, 'detached-history')
        assert history.project_id is None and history.deleted_project['slug'] == 'deleted'
        assert history.launch_spec.inputs == {'original': True}
        assert db.get(SessionTarget, history.id) is None
        next_project = legacy(db, 'after')
        assert next_project.id == 100
        db.commit()
    engine.dispose()
    # Actual isolated recovery: restore the pre-upgrade SQLite backup, upgrade again.
    recovered = tmp_path / 'recovered.db'
    with sqlite3.connect(backup) as src, sqlite3.connect(recovered) as dst:
        src.backup(dst)
    recovered_url = 'sqlite:///' + str(recovered)
    command.upgrade(alembic_config(recovered_url), 'head')
    engine = make_engine(recovered_url)
    with Session(engine) as db:
        assert db.get(Project, 50).deleting is True
        assert len(inventory(db, db.get(Project, 50))['locations']) == 2
        assert db.get(Project, 99) is None
    engine.dispose()


async def test_launch_api_refuses_ambiguous_primary_before_publication(api, project):
    from helpers import write_db

    from acc.server.source_models import ProjectRepository, RepositoryLocator
    with write_db(api.app.state.db) as db:
        db.add(RepositoryLocator(id='extra-locator', authority='person:@api', locator='alex/extra'))
        db.flush()
        db.add(ProjectRepository(id='extra', project_id=project['id'], legacy_project_id=project['id'],
            locator_id='extra-locator', visibility_domain='person:@api', compatibility_primary=False))
        db.commit()
    response = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude', 'name': 'Refuse'})
    assert response.status_code == 409, response.text
    assert 'multiple' in response.json()['detail']
    assert (await api.get('/api/sessions')).json() == []


def test_cross_project_membership_and_account_host_are_rejected(api):
    from sqlalchemy.exc import IntegrityError

    from acc.server.source_models import ExecutionHost, ProjectLocation, ProjectRepository, WorkingLocation
    with api.app.state.db() as db:
        one = legacy(db, 'one', remote_url='https://example.com/alex/one.git')
        two = legacy(db, 'two', remote_url='https://example.com/alex/two.git')
        db.commit()
        membership = db.scalar(select(ProjectRepository).where(ProjectRepository.project_id == two.id))
        link = db.scalar(select(ProjectLocation).where(ProjectLocation.project_id == one.id))
        link.membership_id = membership.id
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        location = db.scalar(select(WorkingLocation))
        db.add(ExecutionHost(id='different', legacy_name='other', kind='unresolved'))
        db.commit()
        location.host_id = 'different'
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


def test_placement_id_reuse_never_reattaches_detached_custody(api):
    from acc.server.source_models import LocationRegistration, ProjectLocation
    with api.app.state.db() as db:
        project = legacy(db)
        placement = Placement(project_id=project.id, host='edge', account='alex-agent', path='/old', person='alex')
        db.add(placement)
        db.commit()
        old_id = placement.id
        old_registration = db.scalar(select(LocationRegistration).where(LocationRegistration.placement_id == old_id))
        old_link = db.scalar(select(ProjectLocation).where(ProjectLocation.registration_id == old_registration.id))
        db.delete(placement)
        db.commit()
        replacement = Placement(id=old_id, project_id=project.id, host='other', account='casey-agent', path='/new', person='casey')
        db.add(replacement)
        db.commit()
        db.refresh(old_registration)
        assert old_registration.placement_id is None
        new_registration = db.scalar(select(LocationRegistration).where(LocationRegistration.placement_id == old_id))
        assert new_registration.id != old_registration.id
        assert new_registration.location_id != old_link.location_id
        assert old_registration.provenance['path'] == '/old'


def test_unverified_legacy_host_retains_routing_after_rename_and_replacement(api):
    from acc.server.models import Node
    from acc.server.sources import compatibility_target
    with api.app.state.db() as db:
        node = Node(id='node1', name='edge', status='active', public_key='key-one')
        db.add(node)
        db.flush()
        project = Project(slug='remote', name='Remote', host='edge', account='alex-agent',
            workspace_path='/work/demo', owner='alex')
        db.add(project)
        db.commit()
        original = compatibility_target(db, project)
        node.name = project.host = 'renamed-edge'
        db.commit()
        renamed = compatibility_target(db, project)
        assert renamed['host_id'] == original['host_id']
        assert renamed['host'] == 'renamed-edge'
        node.public_key = 'replacement-key'
        db.commit()
        assert compatibility_target(db, project)['host'] == 'renamed-edge'
        db.delete(node)
        db.commit()
        assert compatibility_target(db, project)['host'] == 'renamed-edge'


async def test_project_deletion_retains_detached_registration_without_new_custody(api, project):
    from acc.server.source_models import LocationRegistration, ProjectLocation
    with api.app.state.db() as db:
        registration = db.scalar(select(LocationRegistration).where(LocationRegistration.project_id == project['id']))
        ident, location = registration.id, registration.location_id
    response = await api.post(f"/api/projects/{project['id']}/delete", json={'confirm': project['slug']})
    assert response.status_code == 200, response.text
    with api.app.state.db() as db:
        held = db.get(LocationRegistration, ident)
        assert held.project_id is None and held.legacy_project_id == project['id']
        assert held.location_id == location and held.provenance['managed_checkout'] is False
        assert db.scalar(select(ProjectLocation).where(ProjectLocation.registration_id == ident)).project_id is None


def test_upgrade_backfill_failure_rolls_back_and_retries_without_duplicates(tmp_path):
    from sqlalchemy import event, inspect

    from acc.server.db import migrate
    from acc.server.source_models import LocationRegistration
    url = 'sqlite:///' + str(tmp_path / 'interrupted.db')
    cfg = alembic_config(url)
    command.upgrade(cfg, '0081')
    engine = make_engine(url)
    with engine.begin() as db:
        db.execute(Project.__table__.insert().values(id=7, slug='old', name='Old', owner='alex', host='local',
            account='alex-agent', workspace_path='/work/old', import_mode='readonly', managed_checkout=False))
    from sqlalchemy.engine import Engine

    def interrupt(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith('INSERT INTO location_registration'):
            raise RuntimeError('injected interruption')

    event.listen(Engine, 'before_cursor_execute', interrupt)
    try:
        with pytest.raises(RuntimeError, match='injected interruption'):
            migrate(url)
    finally:
        event.remove(Engine, 'before_cursor_execute', interrupt)
    assert 'working_location' not in inspect(engine).get_table_names()
    with engine.connect() as db:
        assert db.exec_driver_sql('SELECT version_num FROM alembic_version').scalar() == '0081'
    migrate(url)
    with Session(engine) as db:
        rows = list(db.scalars(select(LocationRegistration)))
        assert len(rows) == 1 and rows[0].legacy_project_id == 7
    engine.dispose()


async def test_worktree_session_freezes_actual_location_not_project_default(api):
    from helpers import pause_supervisor

    from acc.server.models import Session as AgentSession
    from acc.server.source_models import SessionTarget, WorkingLocation
    await pause_supervisor(api)
    created = await api.post('/api/projects/new', json={'name': 'Frozen worktree', 'source': 'empty'})
    assert created.status_code == 201, created.text
    project = created.json()
    response = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude',
                                                   'name': 'Own tree', 'worktree': True})
    assert response.status_code == 201, response.text
    with api.app.state.db() as db:
        session = db.get(AgentSession, response.json()['id'])
        target = db.get(SessionTarget, session.id)
        assert db.get(WorkingLocation, target.location_id).recorded_path == session.cwd
        assert target.snapshot['path'] == session.cwd
        assert target.snapshot['compatibility_origin']['path'] == project['workspace_path']


async def test_resume_refuses_mutated_frozen_placement_but_not_new_project_default(api, project):
    from helpers import pause_supervisor, write_db

    from acc.server.models import Session as AgentSession
    await pause_supervisor(api)
    response = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude', 'name': 'Pinned'})
    assert response.status_code == 201, response.text
    ident = response.json()['id']
    with write_db(api.app.state.db) as db:
        session = db.get(AgentSession, ident)
        session.project.workspace_path = '/different-default'
        db.commit()
    assert (await api.post(f'/api/sessions/{ident}/resume', json={})).status_code == 200
    with write_db(api.app.state.db) as db:
        db.get(AgentSession, ident).placement.path = '/swapped-location'
        db.commit()
    response = await api.post(f'/api/sessions/{ident}/resume', json={})
    assert response.status_code == 409, response.text
    assert 'recorded location changed' in response.json()['detail']


async def test_automatic_start_refuses_changed_frozen_location(api, project, monkeypatch):
    from helpers import pause_supervisor, write_db

    from acc.server.models import Session as AgentSession
    await pause_supervisor(api)
    broker = api.app.state.supervisor.broker
    original = broker.call

    async def changed_before_dispatch(method, **values):
        result = await original(method, **values)
        if method == 'trust_workspace':
            # The session and its frozen target are published, but dispatch has
            # not happened yet: simulate a stale routing writer during this await.
            with write_db(api.app.state.db) as db:
                session = db.scalar(select(AgentSession).where(AgentSession.project_id == project['id']))
                session.placement.path = '/unreviewed-location'
                db.commit()
        return result

    monkeypatch.setattr(broker, 'call', changed_before_dispatch)
    response = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude', 'name': 'Frozen'})
    assert response.status_code == 201, response.text
    ident = response.json()['id']
    with api.app.state.db() as db:
        session = db.get(AgentSession, ident)
        assert session.observed_state == 'recovery_needed'
        assert 'recorded location changed' in session.last_error
        assert session.runs == []


async def test_identity_read_routes_accept_no_agent_or_anonymous_authority(api, project):
    import httpx
    async with httpx.AsyncClient(transport=api._transport, base_url='http://acc') as anonymous:
        for suffix in ('repositories', 'locations'):
            path = f"/api/projects/{project['id']}/{suffix}"
            assert (await anonymous.get(path)).status_code == 401
            assert (await anonymous.get(path, headers={'Authorization': 'Bearer not-an-installation-token'})).status_code == 401


def test_execution_host_registration_tolerates_concurrent_first_writer(tmp_path):
    from sqlalchemy import event

    from acc.server.source_models import ExecutionHost, WorkingLocation
    url = 'sqlite:///' + str(tmp_path / 'host-race.db')
    command.upgrade(alembic_config(url), 'head')
    engine = make_engine(url)
    inserted = False

    def competing_registration(conn, cursor, statement, parameters, context, executemany):
        nonlocal inserted
        if not inserted and statement.startswith('INSERT INTO source_execution_host'):
            inserted = True
            conn.execute(ExecutionHost.__table__.insert().values(id='competing-host', legacy_name='local',
                kind='local', installation_key='local'))

    event.listen(engine, 'before_cursor_execute', competing_registration)
    try:
        with Session(engine) as db:
            legacy(db)
            db.commit()
            hosts = list(db.scalars(select(ExecutionHost)))
            assert len(hosts) == 1 and hosts[0].id == 'competing-host'
            assert db.scalar(select(WorkingLocation)).host_id == hosts[0].id
    finally:
        event.remove(engine, 'before_cursor_execute', competing_registration)
        engine.dispose()


def test_placement_does_not_inherit_project_folder_creation_rights(api):
    from acc.server.sources import inventory
    with api.app.state.db() as db:
        project = legacy(db, managed_checkout=True)
        db.add(Placement(project_id=project.id, person='casey', host='edge', account='casey-agent', path='/imported'))
        db.commit()
        locations = inventory(db, project)['locations']
        origin = next(row for row in locations if row['placement_id'] is None)
        placed = next(row for row in locations if row['placement_id'] is not None)
        assert origin['provenance']['managed_checkout'] is True
        assert placed['provenance']['managed_checkout'] is False
        from acc.server.source_models import LocationRegistration
        assert db.get(LocationRegistration, placed['registration_id']).provenance['project_managed_checkout'] is True


@pytest.mark.parametrize('initial', [None, 'https://git.example.com/team/old.git'])
async def test_issue_source_remote_change_keeps_legacy_project_launchable(api, project, initial):
    if initial:
        response = await api.post(f"/api/projects/{project['id']}/issue-sources", json={'remote_url': initial})
        assert response.status_code == 200, response.text
    response = await api.post(f"/api/projects/{project['id']}/issue-sources",
                              json={'remote_url': 'https://git.example.com/team/new.git'})
    assert response.status_code == 200, response.text
    launched = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude', 'name': 'After remote change'})
    assert launched.status_code == 201, launched.text
    from acc.server.models import AuditLog
    with api.app.state.db() as db:
        changes = list(db.scalars(select(AuditLog).where(AuditLog.action == 'project.source_rebind')))
        assert changes and all(row.actor == '@api' for row in changes)






@pytest.mark.parametrize('state', ['verified', 'multiple'])
async def test_legacy_remote_writer_refuses_verified_or_multiple_sources(api, project, state):
    from helpers import write_db

    from acc.server.source_models import HostingInstance, ProjectRepository, Repository
    saved = await api.post(f"/api/projects/{project['id']}/issue-sources", json={'remote_url': 'https://git.example.com/team/original.git'})
    assert saved.status_code == 200, saved.text
    with write_db(api.app.state.db) as db:
        member = db.scalar(select(ProjectRepository).where(ProjectRepository.project_id == project['id']))
        if state == 'verified':
            db.add(HostingInstance(id='verified-host', provider='github', api_origin='https://api.github.com'))
            db.flush()
            db.add(Repository(id='verified-repo', instance_id='verified-host', provider_repository_id='42'))
            db.flush()
            member.repository_id, member.locator_id = 'verified-repo', None
        else:
            db.add(ProjectRepository(id='other-source', project_id=project['id'], legacy_project_id=project['id'],
                locator_id=member.locator_id, visibility_domain='person:@api', compatibility_primary=False))
        db.commit()
    refused = await api.post(f"/api/projects/{project['id']}/issue-sources", json={'remote_url': 'https://git.example.com/team/other.git'})
    assert refused.status_code == 409, refused.text
    with api.app.state.db() as db:
        assert db.get(Project, project['id']).remote_url == 'https://git.example.com/team/original.git'


async def test_legacy_source_rebind_preserves_frozen_history_and_supports_removal(api, project):

    from acc.server.models import Session as AgentSession
    from acc.server.source_models import ProjectRepository, SessionTarget
    from acc.server.sources import session_problem
    url = f"/api/projects/{project['id']}/issue-sources"
    assert (await api.post(url, json={'remote_url': 'https://git.example.com/team/original.git'})).status_code == 200
    started = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude', 'name': 'Original source'})
    assert started.status_code == 201, started.text
    ident = started.json()['id']
    with api.app.state.db() as db:
        target = db.get(SessionTarget, ident)
        old_member = target.membership_id
        old_snapshot = target.snapshot
    for remote in ['https://git.example.com/team/new.git', None, 'https://git.example.com/team/again.git']:
        saved = await api.post(url, json={'remote_url': remote})
        assert saved.status_code == 200, saved.text
        launched = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude', 'name': 'After change'})
        assert launched.status_code == 201, launched.text
    with api.app.state.db() as db:
        assert db.get(SessionTarget, ident).snapshot == old_snapshot
        assert db.get(ProjectRepository, old_member).project_id is None
        assert session_problem(db, db.get(AgentSession, ident)) is None
