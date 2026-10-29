"""Owned-event schedules reproduce conversion/deletion races without timing sleeps."""
import asyncio

import pytest
from helpers import wait_for
from test_workspace_delete_recovery import archived_workspace

from acc.server.models import Project, Session


def waiting(supervisor, path):
    operations = getattr(supervisor, 'workspace_operations', None)
    return operations is not None and operations.pending(str(path)) > 0


@pytest.mark.parametrize('operation', ['development', 'promote', 'register', 'register_alias', 'launch', 'resume'])
async def test_delete_holds_workspace_through_broker_outcome(api, env, monkeypatch, operation):
    row, path = await archived_workspace(api, env)
    supervisor = api.app.state.supervisor
    client = supervisor.broker
    real_call = client.call
    entered, release = asyncio.Event(), asyncio.Event()

    async def paused(method, **params):
        if method == 'delete_workspace':
            entered.set()
            await release.wait()
        return await real_call(method, **params)

    monkeypatch.setattr(client, 'call', paused)
    deletion = asyncio.create_task(api.post(f'/api/workspaces/{path.name}/delete', json={'confirm': path.name}))
    await asyncio.wait_for(entered.wait(), 10)
    if operation == 'development':
        request = api.post(f'/api/workspaces/{path.name}/development')
    elif operation == 'promote':
        request = api.post(f"/api/sessions/{row['id']}/promote", json={'name': 'Kept', 'slug': 'kept', 'github': False})
    elif operation in ('register', 'register_alias'):
        registration_path = path
        if operation == 'register_alias':
            alias = env['workspaces'] / 'concurrent-alias'
            alias.symlink_to(path.parent, target_is_directory=True)
            registration_path = alias / path.name
        request = api.post('/api/projects', json={'name': 'Kept', 'slug': 'kept', 'workspace_path': str(registration_path)})
    elif operation == 'launch':
        request = api.post('/api/sessions', json={'name': 'Again', 'provider': 'claude', 'profile': 'researcher', 'workspace': path.name})
    else:
        request = api.post(f"/api/sessions/{row['id']}/resume")
    other = asyncio.create_task(request)
    try:
        assert await wait_for(lambda: other.done() or waiting(supervisor, path))
        assert not other.done(), 'the competing operation must wait, including before mutable-state validation'
    finally:
        release.set()
        deleted, result = await asyncio.gather(deletion, other)
    assert deleted.status_code == 200, deleted.text
    assert result.status_code in (404, 409), result.text
    assert not path.exists()
    with api.app.state.db() as db:
        assert db.query(Project).filter_by(workspace_path=str(path)).count() == 0
        assert db.query(Session).filter_by(workdir=str(path), desired_state='running').count() == 0


async def test_conversion_wins_then_delete_rechecks_project(api, env, monkeypatch):
    _, path = await archived_workspace(api, env)
    supervisor = api.app.state.supervisor
    real_call = supervisor.broker.call
    entered, release = asyncio.Event(), asyncio.Event()

    async def paused(method, **params):
        if method == 'prepare_private_workspace':
            entered.set()
            await release.wait()
        return await real_call(method, **params)

    monkeypatch.setattr(supervisor.broker, 'call', paused)
    conversion = asyncio.create_task(api.post(f'/api/workspaces/{path.name}/development'))
    await asyncio.wait_for(entered.wait(), 10)
    deletion = asyncio.create_task(api.post(f'/api/workspaces/{path.name}/delete', json={'confirm': path.name}))
    try:
        assert await wait_for(lambda: deletion.done() or waiting(supervisor, path))
        assert not deletion.done()
    finally:
        release.set()
        converted, deleted = await asyncio.gather(conversion, deletion)
    assert converted.status_code == 200, converted.text
    assert deleted.status_code == 409 and 'project' in deleted.text
    assert path.is_dir()


async def test_reconcile_defers_busy_workspace_without_waiting_on_supervisor_lock(api, env, monkeypatch):
    from helpers import write_db

    row, path = await archived_workspace(api, env)
    supervisor = api.app.state.supervisor
    with write_db(api.app.state.db) as db:
        session = db.get(Session, row['id'])
        session.desired_state, session.observed_state = 'running', 'idle'
        db.commit()

    async def unexpected_broker(*args, **kwargs):
        raise AssertionError('busy workspace must not reach a broker operation')

    monkeypatch.setattr(supervisor.broker, 'call', unexpected_broker)
    async with supervisor.workspace_operations.hold(str(path)):
        async with supervisor._lock:
            with api.app.state.db() as db:
                session = db.get(Session, row['id'])
                attempts = session.recovery_attempts
                await asyncio.wait_for(supervisor._reconcile_one(db, session, None), 5)
                assert session.recovery_attempts == attempts and session.desired_state == 'running'


async def test_alias_registration_then_delete_preserves_project(api, env):
    _, path = await archived_workspace(api, env)
    alias = env['workspaces'] / 'research-alias'
    alias.symlink_to(path.parent, target_is_directory=True)
    registered = await api.post('/api/projects', json={
        'name': 'Kept research', 'slug': 'kept-research', 'workspace_path': str(alias / path.name)})
    assert registered.status_code == 201, registered.text
    assert registered.json()['workspace_path'] == str(path)
    deleted = await api.post(f'/api/workspaces/{path.name}/delete', json={'confirm': path.name})
    assert deleted.status_code == 409, deleted.text
    assert path.is_dir()


async def test_registration_refuses_when_identity_broker_is_unavailable(api, env, monkeypatch):
    from acc.server.broker_client import BrokerError

    _, path = await archived_workspace(api, env)
    broker = api.app.state.supervisor.broker
    original = broker.call

    async def unavailable(method, **params):
        if method == 'workspace_identity':
            assert params['account'] == env['settings'].default_account
            raise BrokerError('unknown method workspace_identity')
        return await original(method, **params)

    monkeypatch.setattr(broker, 'call', unavailable)
    result = await api.post('/api/projects', json={
        'name': 'Kept research', 'slug': 'kept-research', 'workspace_path': str(path)})
    assert result.status_code == 409, result.text
    with api.app.state.db() as db:
        assert db.query(Project).filter_by(slug='kept-research').count() == 0


@pytest.mark.parametrize('kind', ['launch', 'resume', 'convert', 'other'])
async def test_registration_under_busy_nondestructive_ancestor_succeeds(api, env, kind):
    parent = env['workspaces'] / 'launching-parent'
    child = parent / 'new-project'
    child.mkdir(parents=True)
    async with api.app.state.supervisor.workspace_operations.hold(str(parent), kind=kind):
        response = await api.post('/api/projects', json={
            'name': 'Nested project', 'slug': 'nested-project', 'workspace_path': str(child)})
    assert response.status_code == 201, response.text


@pytest.mark.parametrize('removed', [False, True])
async def test_registration_waits_for_ancestor_delete_and_rechecks(api, env, monkeypatch, removed):
    import shutil

    parent = env['workspaces'] / 'deleting-parent'
    child = parent / 'nested-project'
    child.mkdir(parents=True)
    operations = api.app.state.supervisor.workspace_operations
    original_wait = operations.wait_for_deletions
    waiting = asyncio.Event()

    async def observed_wait(*args, **kwargs):
        waiting.set()
        return await original_wait(*args, **kwargs)

    monkeypatch.setattr(operations, 'wait_for_deletions', observed_wait)
    async with operations.hold(str(parent), kind='delete'):
        request = asyncio.create_task(api.post('/api/projects', json={
            'name': 'Nested project', 'slug': 'nested-project', 'workspace_path': str(child)}))
        await asyncio.wait_for(waiting.wait(), 5)
        assert not request.done()
        if removed:
            shutil.rmtree(parent)
    response = await request
    assert response.status_code == (409 if removed else 201), response.text
    with api.app.state.db() as db:
        assert db.query(Project).filter_by(slug='nested-project').count() == (0 if removed else 1)


async def test_registration_refuses_persistent_ancestor_deletion_with_bounded_wait(api, env, monkeypatch):
    from acc.server import workspace_operations

    monkeypatch.setattr(workspace_operations, 'ANCESTOR_DELETE_WAIT_SECONDS', 0)
    parent = env['workspaces'] / 'deleting-parent'
    child = parent / 'nested-project'
    child.mkdir(parents=True)
    async with api.app.state.supervisor.workspace_operations.hold(str(parent), kind='delete'):
        response = await api.post('/api/projects', json={
            'name': 'Nested project', 'slug': 'nested-project', 'workspace_path': str(child)})
    assert response.status_code == 409 and 'An enclosing folder' in response.text


async def test_registration_rechecks_delete_started_during_post_wait_probe(api, env, monkeypatch):
    parent = env['workspaces'] / 'restarted-delete'
    child = parent / 'nested-project'
    child.mkdir(parents=True)
    operations = api.app.state.supervisor.workspace_operations
    original_wait = operations.wait_for_deletions
    original_call = api.app.state.supervisor.broker.call
    waiting = asyncio.Event()
    second_delete = operations.hold(str(parent), kind='delete')
    probing = False

    async def observed_wait(*args, **kwargs):
        nonlocal probing
        waiting.set()
        result = await original_wait(*args, **kwargs)
        probing = True
        return result

    async def interleaved(method, **params):
        if method == 'scan_docs' and params.get('path') == str(child) and probing:
            await second_delete.__aenter__()
        return await original_call(method, **params)

    monkeypatch.setattr(operations, 'wait_for_deletions', observed_wait)
    monkeypatch.setattr(api.app.state.supervisor.broker, 'call', interleaved)
    async with operations.hold(str(parent), kind='delete'):
        request = asyncio.create_task(api.post('/api/projects', json={
            'name': 'Nested project', 'slug': 'nested-project', 'workspace_path': str(child)}))
        await asyncio.wait_for(waiting.wait(), 5)
    try:
        response = await request
        assert response.status_code == 409 and 'An enclosing folder' in response.text
        with api.app.state.db() as db:
            assert db.query(Project).filter_by(slug='nested-project').count() == 0
    finally:
        await second_delete.__aexit__(None, None, None)


async def test_registration_succeeds_during_actual_ancestor_launch(api, env, monkeypatch):
    from pathlib import Path

    parent = (await api.post('/api/projects/new', json={'name': 'Launching parent', 'source': 'empty'})).json()
    child = Path(parent['workspace_path']) / 'child'
    child.mkdir()
    broker = api.app.state.supervisor.broker
    original_call = broker.call
    entered, release = asyncio.Event(), asyncio.Event()

    async def paused(method, **params):
        if method == 'launch':
            entered.set()
            await release.wait()
        return await original_call(method, **params)

    monkeypatch.setattr(broker, 'call', paused)
    launching = asyncio.create_task(api.post('/api/sessions', json={
        'project_id': parent['id'], 'provider': 'claude', 'name': 'Paused launch'}))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        response = await api.post('/api/projects', json={
            'name': 'Nested project', 'slug': 'nested-project', 'workspace_path': str(child)})
        assert response.status_code == 201, response.text
        assert not launching.done(), 'registration must finish while the ancestor launch is still held'
    finally:
        release.set()
        result = await launching
    assert result.status_code == 201, result.text
