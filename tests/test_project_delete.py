"""Permanent project removal uses isolated brokers; never a live account."""
import pytest
from sqlalchemy import select
from test_github import gh  # noqa: F401 (fixture)

from acc.server.models import AuditLog, PortReservation, Project


async def test_registered_folder_is_kept_and_confirmation_is_exact(api, project, env):
    path = env['workspaces'] / 'demo'
    marker = path / 'keep.txt'
    marker.write_text('registered')
    address = f'/api/projects/{project["id"]}/delete'
    plan = await api.get(address)
    assert plan.status_code == 200, plan.text
    assert plan.json()['remove_folder'] is False
    assert (await api.post(address, json={'confirm': 'wrong'})).status_code == 422
    with api.app.state.db() as db:
        db.add(PortReservation(port=19321, project_id=project['id']))
        db.commit()
    result = await api.post(address, json={'confirm': 'demo'})
    assert result.status_code == 200, result.text
    assert marker.read_text() == 'registered'
    with api.app.state.db() as db:
        assert db.get(Project, project['id']) is None
        assert db.get(PortReservation, 19321) is None
        assert db.scalar(select(AuditLog).where(AuditLog.action == 'project.delete'))


async def test_created_checkout_is_removed(api, env):
    created = await api.post('/api/projects/new', json={'name': 'Disposable', 'source': 'empty'})
    assert created.status_code == 201, created.text
    p = created.json()
    path = env['workspaces'] / p['slug']
    assert path.is_dir()
    result = await api.post(f'/api/projects/{p["id"]}/delete', json={'confirm': p['slug']})
    assert result.status_code == 200, result.text
    assert not path.exists()


async def test_live_session_refused_then_archived_history_detached(api, project, env, monkeypatch):
    launched = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude', 'name': 'history'})
    assert launched.status_code == 201, launched.text
    sid = launched.json()['id']
    address = f'/api/projects/{project["id"]}/delete'
    assert (await api.post(address, json={'confirm': 'demo'})).status_code == 409
    stopped = await api.post(f'/api/sessions/{sid}/stop')
    assert stopped.status_code == 200, stopped.text
    broker = api.app.state.supervisor.broker
    original = broker.call
    async def unconfirmed(method, **kwargs):
        if method == 'list':
            return [{'tmux_name': launched.json()['tmux_name'], 'dead': False}]
        return await original(method, **kwargs)
    monkeypatch.setattr(broker, 'call', unconfirmed)
    assert (await api.post(address, json={'confirm': 'demo'})).status_code == 409
    monkeypatch.setattr(broker, 'call', original)
    result = await api.post(address, json={'confirm': 'demo'})
    assert result.status_code == 200, result.text
    history = await api.get(f'/api/sessions/{sid}')
    assert history.status_code == 200, history.text
    assert history.json()['project_id'] is None
    assert history.json()['project'] == 'demo'
    assert history.json()['desired_state'] == 'archived'
    listing = await api.get('/api/sessions?archived=true')
    assert listing.status_code == 200, listing.text
    resumed = await api.post(f'/api/sessions/{sid}/resume')
    assert resumed.status_code == 409, resumed.text


async def test_published_site_and_pending_request_refuse(api, project):
    from acc.server.models import PublishRequest
    address = f'/api/projects/{project["id"]}/delete'
    for state in ('applied', 'pending_approval'):
        with api.app.state.db() as db:
            db.merge(PublishRequest(id='site', action='publish', host='demo.example.com', requested_by='api',
                                    project_id=project['id'], status=state))
            db.commit()
        result = await api.post(address, json={'confirm': 'demo'})
        assert result.status_code == 409 and 'publish' in result.text.lower()


@pytest.mark.parametrize('kind', ['symlink', 'alias', 'outside'])
async def test_unsafe_managed_folder_is_refused(api, env, kind):
    from pathlib import Path
    created = (await api.post('/api/projects/new', json={'name': 'Disposable', 'source': 'empty'})).json()
    path = Path(created['workspace_path'])
    if kind == 'symlink':
        moved = path.with_name('kept')
        path.rename(moved)
        path.symlink_to(moved, target_is_directory=True)
    else:
        with api.app.state.db() as db:
            p = db.get(Project, created['id'])
            p.workspace_path = str(path.parent) + '/other/../' + path.name if kind == 'alias' else str(env['home'])
            db.commit()
    result = await api.post(f'/api/projects/{created["id"]}/delete', json={'confirm': created['slug']})
    assert result.status_code == 409, result.text
    assert path.exists()


async def test_remote_offline_is_clear_and_preserves_record(api, project):
    with api.app.state.db() as db:
        db.get(Project, project['id']).host = 'offline-example'
        db.commit()
    result = await api.post(f'/api/projects/{project["id"]}/delete', json={'confirm': 'demo'})
    assert result.status_code == 409 and 'offline' in result.text.lower()
    with api.app.state.db() as db:
        assert db.get(Project, project['id'])


async def test_member_only_deletes_own_project(api, project, env, member_accounts):
    from test_auth import cookie_client, do_setup
    from test_users import browser, csrf, join
    await do_setup(api, env)
    owner = cookie_client(api)
    link = (await owner.post('/api/users/invites', json={'role': 'member', 'account': 'dana-agent'}, headers=csrf(owner))).json()['link']
    async with browser(api) as member:
        assert (await join(member, link, 'alex')).status_code == 200
        for method in ('get', 'post'):
            kwargs = {'json': {'confirm': 'demo'}, 'headers': csrf(member)} if method == 'post' else {}
            assert (await getattr(member, method)(f'/api/projects/{project["id"]}/delete', **kwargs)).status_code == 404
        created = await member.post('/api/projects/new', json={'name': 'Member disposable', 'source': 'empty'}, headers=csrf(member))
        assert created.status_code == 201, created.text
        p = created.json()
        result = await member.post(f'/api/projects/{p["id"]}/delete', json={'confirm': p['slug']}, headers=csrf(member))
        assert result.status_code == 200, result.text


async def test_managed_worktrees_removed_including_dirty_files(api, env):
    from pathlib import Path

    from acc.broker import ops
    p = (await api.post('/api/projects/new', json={'name': 'Worktree disposal', 'source': 'empty'})).json()
    made = ops.make_worktree(env['broker'], p['workspace_path'], str(env['workspaces']),
                             'disposal-abc12345', 'acc/disposal-abc12345')
    tree = Path(made['path'])
    (tree / 'dirty.txt').write_text('confirmed loss')
    address = f'/api/projects/{p["id"]}/delete'
    plan = (await api.get(address)).json()
    assert str(tree) in plan['folders']
    response = await api.post(address, json={'confirm': p['slug']})
    assert response.status_code == 200, response.text
    assert not tree.exists()


async def test_readonly_created_checkout_stays(api, env):
    from pathlib import Path
    p = (await api.post('/api/projects/new', json={'name': 'Readonly disposal', 'source': 'empty'})).json()
    with api.app.state.db() as db:
        db.get(Project, p['id']).import_mode = 'readonly'
        db.commit()
    response = await api.post(f'/api/projects/{p["id"]}/delete', json={'confirm': p['slug']})
    assert response.status_code == 200, response.text
    assert Path(p['workspace_path']).is_dir()


async def test_running_compose_stack_refused(api, project, env, monkeypatch):
    from acc.broker import ops
    (env['workspaces'] / 'demo' / 'compose.yaml').write_text('services: {}')
    monkeypatch.setattr(ops, 'compose_ps', lambda *_: [{'State': 'running'}])
    with api.app.state.db() as db:
        db.get(Project, project['id']).compose_pin = {'digest': 'x'}
        db.commit()
    result = await api.post(f'/api/projects/{project["id"]}/delete', json={'confirm': 'demo'})
    assert result.status_code == 409 and 'running containers' in result.text


async def test_archive_barrier_refuses_launch_during_broker_checks(api, env, monkeypatch):
    from acc.server.compiler import LaunchInputs
    from acc.server.supervisor import LaunchRefused
    p = (await api.post('/api/projects/new', json={'name': 'Launch barrier', 'source': 'empty'})).json()
    broker = api.app.state.supervisor.broker
    original = broker.call
    checked = False
    async def call(method, **kwargs):
        nonlocal checked
        if method == 'project_delete_identity':
            checked = True
            with pytest.raises(LaunchRefused, match='archived'):
                await api.app.state.supervisor.create_session(p['id'], LaunchInputs(provider='claude'), 'late', 'api')
        return await original(method, **kwargs)
    monkeypatch.setattr(broker, 'call', call)
    response = await api.post(f'/api/projects/{p["id"]}/delete', json={'confirm': p['slug']})
    assert response.status_code == 200, response.text
    assert checked


async def test_retains_searchable_history_inbox_and_multirun(api, project, env):
    from acc.server import history
    from acc.server.models import AttentionItem, LaunchSpec, MultiRun, MultiRunEntry, Session, Setting, ToolRequest
    with api.app.state.db() as db:
        spec = LaunchSpec(provider='claude', inputs={}, files={}, digest='a')
        db.add(spec)
        db.flush()
        db.add(Session(id='kept', project_id=project['id'], provider='claude', name='Kept conversation',
                       tmux_name='acc-kept-test', launch_spec_id=spec.id, run_as=env['settings'].default_account,
                       desired_state='stopped', observed_state='stopped'))
        db.add(MultiRun(id='multi-kept', project_id=project['id'], name='Comparison'))
        db.flush()
        db.add(MultiRunEntry(group_id='multi-kept', position=0, choice={}, session_id='kept'))
        db.add(AttentionItem(dedupe_key='keep-alert', kind='test', severity='info', source='test', title='Keep alert'))
        db.add(ToolRequest(id='keep-request', project_id=project['id'], owner='api', account='example-agent', host='local',
                           manager='npm', name='example', operation='install', reason='Keep pending approval'))
        db.add(Setting(key=f'tools.project.{project["id"]}', value={'required': True}))
        history.replace(db, 'kept', [{'text': 'Remember the lighthouse', 'role': 'assistant'}])
        db.commit()
    response = await api.post(f'/api/projects/{project["id"]}/delete', json={'confirm': 'demo'})
    assert response.status_code == 200, response.text
    found = await api.get('/api/search?q=lighthouse')
    assert found.status_code == 200 and found.json()['results'][0]['session']['project'] == 'demo'
    multi = await api.get('/api/multiruns/multi-kept')
    assert multi.status_code == 200, multi.text
    with api.app.state.db() as db:
        assert db.scalar(select(AttentionItem).where(AttentionItem.dedupe_key == 'keep-alert')).title == 'Keep alert'
        request = db.get(ToolRequest, 'keep-request')
        assert request.status == 'pending_approval' and request.project_id is None
        assert db.get(Setting, f'tools.project.{project["id"]}') is None


async def test_registered_external_folder_kept_without_reading_it(api, env):
    path = env['home'] / 'external-folder'
    path.mkdir()
    (path / 'keep.txt').write_text('external')
    created = await api.post('/api/projects', json={'slug': 'external', 'name': 'External', 'workspace_path': str(path)})
    assert created.status_code == 201, created.text
    p = created.json()
    response = await api.post(f'/api/projects/{p["id"]}/delete', json={'confirm': 'external'})
    assert response.status_code == 200, response.text
    assert (path / 'keep.txt').read_text() == 'external'


async def test_autostart_waits_for_workspace_operation(api, env, monkeypatch):
    p = (await api.post('/api/projects/new', json={'name': 'Compose lock', 'source': 'empty'})).json()
    sup = api.app.state.supervisor
    with api.app.state.db() as db:
        value = db.get(Project, p['id'])
        value.compose_autostart = True
        value.compose_pin = {'digest': 'x'}
        db.commit()
    calls = []
    original = sup.broker.call
    async def call(method, **kwargs):
        if method == 'compose_up':
            calls.append(kwargs)
            return {}
        return await original(method, **kwargs)
    monkeypatch.setattr(sup.broker, 'call', call)
    async with sup.workspace_operations.hold(p['workspace_path']):
        assert not await sup.autostart_projects()
        assert calls == []
    assert await sup.autostart_projects()
    assert len(calls) == 1


def test_broker_pins_identity_and_does_not_follow_inner_links(env):
    from acc.broker import ops
    root = env['workspaces']
    path = root / 'disposable'
    path.mkdir()
    outside = env['home'] / 'keep.txt'
    outside.write_text('keep')
    (path / 'link').symlink_to(outside)
    identity = ops.project_delete_identity(env['broker'], str(path), [])
    moved = root / 'moved'
    path.rename(moved)
    path.mkdir()
    with pytest.raises(ops.OpError, match='identity changed'):
        ops.delete_project_checkout(env['broker'], str(path), [], identity)
    path.rmdir()
    moved.rename(path)
    assert ops.delete_project_checkout(env['broker'], str(path), [], identity)['deleted'] == str(path)
    assert outside.read_text() == 'keep'


async def test_deleted_project_id_is_not_reused(api, project, env):
    assert (await api.post(f'/api/projects/{project["id"]}/delete', json={'confirm': 'demo'})).status_code == 200
    created = await api.post('/api/projects/new', json={'name': 'Next project', 'source': 'empty'})
    assert created.status_code == 201, created.text
    assert created.json()['id'] > project['id']


async def test_deletion_barrier_refuses_new_publish_requests_and_gates(api, project, env, monkeypatch):
    from acc.server.models import Setting
    from acc.server.orchestrator import gates
    from acc.server.publish import PublishRefused
    from acc.server.supervisor import LaunchRefused
    monkeypatch.setenv('ACC_ORCHESTRATOR', '1')
    monkeypatch.setenv('ACC_ORCHESTRATOR_V2', '1')
    with api.app.state.db() as db:
        db.add(Setting(key=gates.BUDGET_KEY, value={'enabled': True}))
        p = db.get(Project, project['id'])
        p.deleting = True
        db.commit()
        with pytest.raises(LaunchRefused, match='being deleted'):
            gates.enqueue(db, p, 'acc/example', 'related', 'check', 'api', settings=env['settings'])
    with pytest.raises(PublishRefused, match='being deleted'):
        api.app.state.publisher.request(action='publish', host='demo.example.com', port=8123,
            access='public', requested_by='api', project_id=project['id'])


async def test_unpublished_site_history_does_not_block_deletion(api, project):
    from acc.server.models import PublishRequest
    with api.app.state.db() as db:
        db.add(PublishRequest(id='old-publish', action='publish', host='demo.example.com', project_id=project['id'],
                              status='removed', requested_by='api'))
        db.add(PublishRequest(id='done-unpublish', action='unpublish', host='demo.example.com', project_id=project['id'],
                              status='applied', requested_by='api'))
        db.commit()
    result = await api.post(f'/api/projects/{project["id"]}/delete', json={'confirm': 'demo'})
    assert result.status_code == 200, result.text


async def test_new_github_checkout_is_managed_but_import_is_retained(api, env, gh):  # noqa: F811
    from pathlib import Path
    created = await api.post('/api/github/repos', json={'name': 'new-disposable', 'confirm': True})
    assert created.status_code == 201, created.text
    p = created.json()
    assert p['managed_checkout'] is True
    deleted = await api.post(f'/api/projects/{p["id"]}/delete', json={'confirm': p['slug']})
    assert deleted.status_code == 200, deleted.text
    assert not Path(p['workspace_path']).exists()
    imported = await api.post('/api/projects/import', json={'repo': 'o/imported', 'trust': True})
    assert imported.status_code == 201, imported.text
    p = imported.json()
    assert not p['managed_checkout']
    assert (await api.post(f'/api/projects/{p["id"]}/delete', json={'confirm': p['slug']})).status_code == 200
    assert Path(p['workspace_path']).is_dir()


async def test_live_team_refused_even_after_lead_session_stops(api, project):
    from datetime import timedelta

    from acc.server.models import Team, utcnow
    launched = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude', 'name': 'Team history'})
    assert launched.status_code == 201, launched.text
    sid = launched.json()['id']
    assert (await api.post(f'/api/sessions/{sid}/stop')).status_code == 200
    with api.app.state.db() as db:
        db.add(Team(id=sid, limits={}, expires_at=utcnow() + timedelta(hours=1), stopped=False))
        db.commit()
    result = await api.post(f'/api/projects/{project["id"]}/delete', json={'confirm': 'demo'})
    assert result.status_code == 409 and 'live team' in result.text
    with api.app.state.db() as db:
        db.get(Team, sid).stopped = True
        db.commit()
    assert (await api.post(f'/api/projects/{project["id"]}/delete', json={'confirm': 'demo'})).status_code == 200
