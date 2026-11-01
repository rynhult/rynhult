"""Owner-only folder metadata and principal-bound previews on isolated servers."""
import pytest


async def verified(api):
    from test_admin_features import owner_browser

    from acc.server import auth
    from acc.server.models import AuthSession
    headers = await owner_browser(api)
    with api.app.state.db() as db:
        db.get(AuthSession, auth.digest(api.cookies.get('acc_session'))).reauthenticated_at = auth.now()
        db.commit()
    return headers


async def test_folder_endpoints_refuse_installation_credentials(api):
    assert (await api.post('/api/security/folders/' + 'a' * 32 + '/recover',
        json={'digest': 'b' * 64, 'recovery_digest': 'c' * 64})).status_code == 403
    assert (await api.post('/api/security/folders/check', json={'machine': 'local', 'account': 'agent', 'path': '/etc'})).status_code == 403
    assert (await api.post('/api/security/folders/preview', json={'machine': 'local', 'account': 'agent', 'roots': ['/etc'], 'revision': 0})).status_code == 403


async def test_folder_check_unknown_account_is_refused_without_filesystem_probe(api):
    headers = await verified(api)
    response = await api.post('/api/security/folders/check', headers=headers,
        json={'machine': 'local', 'account': 'root', 'path': '/etc'})
    assert response.status_code == 422
    assert '/etc' not in response.text


async def test_folder_status_honestly_reports_unprovisioned_helper(api):
    headers = await verified(api)
    response = await api.get('/api/security/folders', headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()['accounts']
    assert all(row['state'] in ('legacy', 'pending', 'applied', 'unavailable') for row in response.json()['accounts'])


async def test_folder_previews_require_recent_human_browser(api):
    from test_admin_features import owner_browser
    headers = await owner_browser(api)
    assert (await api.post('/api/security/folders/' + 'a' * 32 + '/recover', headers=headers,
        json={'digest': 'b' * 64, 'recovery_digest': 'c' * 64})).status_code == 403
    response = await api.post('/api/security/folders/preview', headers=headers,
        json={'machine': 'local', 'account': 'agent', 'roots': ['/home/alex/src/repo'], 'revision': 0})
    assert response.status_code == 403


@pytest.mark.parametrize('lost_ack', [False, True])
async def test_folder_apply_recovery_by_another_verified_owner_after_target_preview_expiry(api, tmp_path, monkeypatch, lost_ack):
    import os

    from acc import folder_channel
    from acc.folder_worker import FolderWorker
    from acc.server.models import SecurityFolderPolicy
    headers = await verified(api)
    account = api.app.state.ctx.settings.default_account
    host = tmp_path / 'host'
    repo = host / 'home/alex/src/repo'
    repo.mkdir(parents=True)
    state = tmp_path / 'worker'
    state.mkdir(mode=0o700)
    mapping = {'machine_id': 'local', 'qualified': False, 'policy_qualified': True, 'accounts': {account: {
        'uid': 12345, 'owner_uid': os.getuid(), 'homes': ['/home/alex'],
        'protected': ['/srv/acc'], 'roots': ['/home/alex-agent/workspaces']}}}
    unavailable = False
    # The root boundary is a fake transport; the actual worker and DB protocol
    # run here. Real privileged broker/UID qualification is a separate VM test.
    with FolderWorker(state, mapping, host_root=host, owner=os.getuid(),
                      reload=lambda _, value: {'revision': value['revision']}) as worker:
        async def target(message):
            if message['action'] == 'policy_apply':
                with api.app.state.db() as db:
                    assert db.query(SecurityFolderPolicy).one().state == 'pending'
                if unavailable:
                    if lost_ack:
                        worker.handle(message)
                    raise OSError('fake disconnection')
            return worker.handle(message)
        monkeypatch.setattr(folder_channel, 'request', target)
        preview = await api.post('/api/security/folders/preview', headers=headers,
            json={'machine': 'local', 'account': account, 'roots': ['/home/alex-agent/workspaces', '/home/alex/src/repo'], 'revision': 0})
        assert preview.status_code == 200, preview.text
        data = preview.json()
        unavailable = True
        applied = await api.post(f"/api/security/folders/{data['id']}/apply", headers=headers, json={'digest': data['digest']})
        assert applied.status_code == 503
        with api.app.state.db() as db:
            row = db.query(SecurityFolderPolicy).one()
            assert row.state == 'pending' and row.applied_revision == 0
        from acc.server.models import SecurityChange
        with api.app.state.db() as db:
            published = db.get(SecurityChange, data['id'])
            published.actor, published.session_hash = 'previous-owner', 'previous-sign-in'
            db.commit()
        old = worker.read(data['id'] + '.json')
        old['expires_at'] = 0
        worker.write(data['id'] + '.json', old)
        saved = await api.get(f"/api/security/folders/operations/{data['id']}", headers=headers)
        assert saved.status_code == 200, saved.text
        assert saved.json()['digest'] == data['digest']
        assert saved.json()['retry_available'] is True
        unavailable = False
        retried = await api.post(f"/api/security/folders/{data['id']}/retry", headers=headers, json={'digest': data['digest']})
        assert retried.status_code == 200, retried.text
        if not lost_ack:
            assert retried.json()['state'] == 'pending_policy'
            with api.app.state.db() as db:
                from acc.server import security_settings as policy
                revision = policy.revision(db)
                assert db.query(SecurityFolderPolicy).one().state == 'pending_policy'
                from acc.server.models import AuditLog
                recovered = db.query(AuditLog).filter_by(action='security.folders.recovered', target=data['id']).one()
                assert recovered.detail['state'] == 'pending_policy'
            fresh = await api.post('/api/security/folders/preview', headers=headers, json={
                'machine': 'local', 'account': account, 'roots': ['/home/alex-agent/workspaces', '/home/alex/src/repo'], 'revision': revision})
            assert fresh.status_code == 200, fresh.text
            new_review = fresh.json()
            delivered = await api.post(f"/api/security/folders/{new_review['id']}/apply", headers=headers, json={'digest': new_review['digest']})
            assert delivered.status_code == 200, delivered.text
            from acc.folder_policy import FolderError
            assert worker.read(data['id'] + '.json')['state'] == 'expired'
            with pytest.raises(FolderError, match='expired|revision changed'):
                worker.handle({'action': 'policy_apply', 'operation_id': data['id'], 'machine_id': 'local',
                    'account': account, 'revision': 0, 'digest': data['digest']})
        with api.app.state.db() as db:
            row = db.query(SecurityFolderPolicy).one()
            assert row.state == 'applied' and row.revision == row.applied_revision == 1
        replay = await api.post(f"/api/security/folders/{data['id']}/apply", headers=headers, json={'digest': data['digest']})
        assert replay.status_code == 404
        # A generic policy endpoint must not reinterpret a folder approval.
        assert (await api.post(f"/api/security/changes/{data['id']}/apply", headers=headers,
            json={'digest': data['digest']})).status_code == 404

        # Both directions, including re-enabling, need a fresh reviewed target
        # acknowledgement. pending_policy must not be mistaken for in-flight IO.
        from acc.server import security_settings as policy
        from acc.server.broker_client import BrokerError
        for allowed in (False, True):
            with api.app.state.db() as db:
                policy.save(db, policy.account_scope(db, 'local', account),
                    {'allowed_folders': allowed}, actor='casey', expected_revision=policy.revision(db), default_account=account)
                db.commit()
                assert db.query(SecurityFolderPolicy).one().state == 'pending_policy'
                with pytest.raises(BrokerError, match='pending'):
                    policy.folder_revision(db, 'local', account)
                revision = policy.revision(db)
            review = await api.post('/api/security/folders/preview', headers=headers, json={
                'machine': 'local', 'account': account, 'roots': ['/home/alex-agent/workspaces', '/home/alex/src/repo'], 'revision': revision})
            assert review.status_code == 200, review.text
            fresh = review.json()
            result = await api.post(f"/api/security/folders/{fresh['id']}/apply", headers=headers,
                json={'digest': fresh['digest']})
            assert result.status_code == 200, result.text
            with api.app.state.db() as db:
                assert policy.resolve(db, machine='local', account=account)['enforcement']['allowed_folders'] == 'applied'
                assert policy.folder_revision(db, 'local', account) == db.query(SecurityFolderPolicy).one().applied_revision


async def test_t3_folder_policy_reload_does_not_require_gate_quiescence(api, project, monkeypatch):
    from acc import folder_channel
    from acc.server.orchestrator.gates import GateJob
    headers = await verified(api)
    account = api.app.state.ctx.settings.default_account
    with api.app.state.db() as db:
        db.add(GateJob(id='c' * 32, project_id=project['id'], branch='main', suite='related', priority='check',
            state='queued', host='local', account=account, requested_by='casey'))
        db.commit()
    async def root(message):
        if message['action'] == 'status':
            return {'policy_recovery': True}
        if message['action'] == 'policy_apply':
            return {'state': 'applied', 'revision': 1}
        return {'digest': 'd' * 64, 'roots': message['roots'], 'legacy_roots': message['roots']}
    monkeypatch.setattr(folder_channel, 'request', root)
    preview = await api.post('/api/security/folders/preview', headers=headers, json={
        'machine': 'local', 'account': account, 'roots': ['/home/alex-agent/workspaces'], 'revision': 0})
    assert preview.status_code == 200, preview.text
    result = preview.json()
    response = await api.post(f"/api/security/folders/{result['id']}/apply", headers=headers, json={'digest': result['digest']})
    assert response.status_code == 200, response.text


async def test_expired_folder_preview_refuses_at_exact_deadline(api, monkeypatch):
    from acc import folder_channel
    from acc.server.api import security_folders
    from acc.server.models import SecurityChange
    headers = await verified(api)
    account = api.app.state.ctx.settings.default_account
    async def root(message):
        if message['action'] == 'status':
            return {'policy_recovery': True}
        if message['action'] != 'policy_preview':
            pytest.fail('An expired preview cannot contact the root worker for apply.')
        return {'digest': 'e' * 64, 'roots': message['roots'], 'legacy_roots': message['roots']}
    monkeypatch.setattr(folder_channel, 'request', root)
    preview = await api.post('/api/security/folders/preview', headers=headers, json={
        'machine': 'local', 'account': account, 'roots': ['/home/alex-agent/workspaces'], 'revision': 0})
    assert preview.status_code == 200
    data = preview.json()
    with api.app.state.db() as db:
        expires = db.get(SecurityChange, data['id']).expires_at
    monkeypatch.setattr(security_folders, 'utcnow', lambda: expires)
    result = await api.post(f"/api/security/folders/{data['id']}/apply", headers=headers, json={'digest': data['digest']})
    assert result.status_code == 409


async def test_offline_account_check_reports_unavailable_not_missing_os_access(api, monkeypatch):
    from acc.server.broker_client import BrokerUnavailable
    headers = await verified(api)
    account = api.app.state.ctx.settings.default_account
    broker = api.app.state.ctx.broker
    call = broker.call
    async def offline(method, **params):
        if method == 'folder_check':
            raise BrokerUnavailable('disposable broker offline')
        return await call(method, **params)
    monkeypatch.setattr(broker, 'call', offline)
    response = await api.post('/api/security/folders/check', headers=headers,
        json={'machine': 'local', 'account': account, 'path': '/home/alex/src/repo'})
    assert response.status_code == 200
    assert response.json()['state'] == 'unavailable'


@pytest.mark.parametrize('path', ['/api/security/folders/preview', '/api/security/access/preview'])
async def test_folder_routes_record_bounded_refusals(api, path):
    import json

    from acc.server.models import AuditLog
    payload = {'machine': 'local', 'account': api.app.state.ctx.settings.default_account, 'revision': 0}
    payload.update({'roots': ['/home/alex/src/repo']} if '/folders/' in path else {'path': '/home/alex/src/repo'})
    response = await api.post(path, json=payload)
    assert response.status_code == 403
    with api.app.state.db() as db:
        rows = db.query(AuditLog).filter_by(action='security.policy.refused').all()
        assert rows
        detail = rows[-1].detail
        assert set(detail) == {'operation', 'outcome', 'status', 'reason', 'revision'}
        assert detail['status'] == 403
        assert 'scope' not in json.dumps(detail)


async def test_folder_inventory_refuses_installation_token(api):
    assert (await api.get('/api/security/folders')).status_code == 403


async def test_folder_inventory_human_owner_read_needs_no_recent_reauth(api):
    from test_admin_features import owner_browser

    from acc.server import auth
    from acc.server.models import AuthSession
    headers = await owner_browser(api)
    with api.app.state.db() as db:
        db.get(AuthSession, auth.digest(api.cookies.get('acc_session'))).reauthenticated_at = None
        db.commit()
    response = await api.get('/api/security/folders', headers=headers)
    assert response.status_code == 200, response.text


@pytest.mark.parametrize('bearer', ['', 'fixture-agent-credential', 'fixture-mcp-credential'])
async def test_folder_inventory_refuses_anonymous_or_non_browser_credentials(api, bearer):
    response = await api.get('/api/security/folders', headers={'Authorization': 'Bearer ' + bearer if bearer else ''})
    assert response.status_code == 401


@pytest.mark.parametrize('unavailable_first', [False, True])
@pytest.mark.parametrize('legacy', [False, True])
async def test_t3_switch_acknowledges_unchanged_roots_with_running_session(api, project, env, tmp_path, monkeypatch, unavailable_first, legacy):
    import json
    import os

    from test_supervisor import launch

    from acc import folder_channel
    from acc.folder_worker import FolderWorker
    from acc.server import security_settings as policy
    from acc.server.compiler import LaunchInputs
    from acc.server.models import SecurityFolderPolicy, Session
    headers = await verified(api)
    session = await launch(api, project)
    account = project['account']
    roots = [str(env['workspaces'])]
    with api.app.state.db() as db:
        scope = policy.account_scope(db, 'local', account)
        if not legacy:
            db.add(SecurityFolderPolicy(key=policy.scope_key(scope), machine_id='local', account=account,
                roots=roots, revision=0, applied_revision=0, state='applied', detail={'allowed_folders': True}))
        db.commit()
    state = tmp_path / 'worker-state'
    state.mkdir(mode=0o700)
    mapping = {'machine_id': 'local', 'qualified': False, 'policy_qualified': True, 'accounts': {account: {
        'uid': 12345, 'owner_uid': os.getuid(), 'homes': ['/home/alex'], 'protected': ['/srv/acc'], 'roots': roots}}}
    acknowledgements = []
    def reload(_, payload):
        acknowledgements.append(payload)
        return {'revision': payload['revision']}
    with FolderWorker(state, mapping, host_root=tmp_path, owner=os.getuid(), reload=reload) as worker:
        failed = False
        async def target(message):
            nonlocal failed
            if unavailable_first and not failed and message['action'] == 'policy_apply':
                failed = True
                raise OSError('fixture target unavailable')
            return worker.handle(message)
        monkeypatch.setattr(folder_channel, 'request', target)
        from acc.server.api import security_folders as routes
        acknowledge = routes.acknowledge_switch
        stale_reviews = []
        async def race_ack(*args, **kwargs):
            current_revision = (await api.get('/api/security/scopes', headers=headers)).json()['revision']
            stale = await api.post('/api/security/folders/preview', headers=headers, json={
                'machine': 'local', 'account': account, 'roots': roots, 'revision': current_revision})
            assert stale.status_code == 200, stale.text
            stale_reviews.append(stale.json())
            return await acknowledge(*args, **kwargs)
        monkeypatch.setattr(routes, 'acknowledge_switch', race_ack)
        for allowed in (False, True):
            revision = (await api.get('/api/security/scopes', headers=headers)).json()['revision']
            preview = await api.post('/api/security/changes/preview', headers=headers, json={
                'scope': {'kind': 'account', 'machine': 'local', 'account': account},
                'revision': revision, 'overrides': {'allowed_folders': allowed}})
            assert preview.status_code == 200, preview.text
            data = preview.json()
            if allowed:
                assert data['older_sessions'] == [session['id']]
            applied = await api.post(f"/api/security/changes/{data['id']}/apply", headers=headers, json={'digest': data['digest']})
            assert applied.status_code == 200, applied.text
            stale_data = stale_reviews[-1]
            refused = await api.post(f"/api/security/folders/{stale_data['id']}/apply", headers=headers,
                json={'digest': stale_data['digest']})
            assert refused.status_code == 409
            with api.app.state.db() as db:
                assert policy.revision(db) == applied.json()['revision'] == revision + (2 if unavailable_first and not allowed else 3)
            if unavailable_first and not allowed:
                assert applied.json()['folder_application']['state'] == 'pending'
                with api.app.state.db() as db:
                    from acc.server.broker_client import BrokerError
                    with pytest.raises(BrokerError, match='pending'):
                        policy.folder_revision(db, 'local', account)
                    pending_row = db.get(SecurityFolderPolicy, policy.scope_key(scope))
                    operation = pending_row.detail['operation_id']
                reviewed = await api.get(f'/api/security/folders/operations/{operation}', headers=headers)
                assert reviewed.status_code == 200
                retried = await api.post(f'/api/security/folders/{operation}/retry', headers=headers,
                                         json={'digest': reviewed.json()['digest']})
                assert retried.status_code == 200, retried.text
            else:
                assert applied.json()['folder_application']['state'] == 'applied'
            with api.app.state.db() as db:
                current = policy.resolve(db, project_id=project['id'])
                assert current['enforcement']['allowed_folders'] == 'applied'
                policy.validate_launch_inputs(LaunchInputs(provider='claude'), current)
                assert db.get(Session, session['id']).desired_state == 'running'
                if not allowed:
                    # Review fixture: a generation launched under the now-acknowledged Off policy.
                    row = db.get(Session, session['id'])
                    frozen = json.loads(row.launch_spec.files['guardrails.json'])
                    frozen['security']['switches']['allowed_folders'] = False
                    row.launch_spec.files = {**row.launch_spec.files, 'guardrails.json': json.dumps(frozen)}
                    db.commit()
                else:
                    assert policy.older_sessions(db, scope) == [session['id']]
        assert [p['allowed_folders'] for p in acknowledgements] == [False, True]
        assert all(p['roots'] == roots for p in acknowledgements)


@pytest.mark.parametrize('qualified', [False, None])
async def test_v1_undeliverable_off_refused_at_preview(api, monkeypatch, qualified):
    from acc import folder_channel
    headers = await verified(api)
    async def unavailable(message):
        if qualified is None:
            raise OSError('offline')
        return {'policy_qualified': qualified}
    monkeypatch.setattr(folder_channel, 'request', unavailable)
    response = await api.post('/api/security/changes/preview', headers=headers, json={
        'scope': {'kind': 'account', 'machine': 'local', 'account': api.app.state.ctx.settings.default_account},
        'revision': 0, 'overrides': {'allowed_folders': False}})
    assert response.status_code == 409
    assert 'cannot be turned Off' in response.json()['detail']


@pytest.mark.parametrize('restored', [{}, {'allowed_folders': True}])
async def test_v1_restore_unpublished_installer_default_removes_fence(api, project, restored):
    from acc.server import security_settings as policy
    from acc.server.models import AuditLog, SecurityFolderPolicy
    account = api.app.state.ctx.settings.default_account
    scope = {'kind': 'account', 'machine': 'local', 'account': account}
    with api.app.state.db() as db:
        policy.save(db, scope, {'allowed_folders': False}, actor='owner', expected_revision=0)
        db.commit()
        policy.save(db, scope, restored, actor='owner', expected_revision=1)
        db.commit()
        assert db.query(SecurityFolderPolicy).count() == 0
        policy.folder_revision(db, 'local', account)
        assert db.query(AuditLog).filter_by(action='security.folders.unpublished_cancelled').one()


async def test_v1_restore_last_acknowledged_off_without_connected_target(api, project, monkeypatch):
    from acc import folder_channel
    from acc.server import security_settings as policy
    from acc.server.models import SecurityFolderPolicy
    headers = await verified(api)
    account = project['account']
    scope = {'kind': 'account', 'machine': 'local', 'account': account}
    with api.app.state.db() as db:
        policy.save(db, scope, {'allowed_folders': False}, actor='owner', expected_revision=0)
        row = db.query(SecurityFolderPolicy).one()
        row.state, row.revision, row.applied_revision = 'applied', 2, 2
        row.detail = {'allowed_folders': False, 'operation_id': 'a' * 32}
        policy.save(db, scope, {'allowed_folders': True}, actor='owner', expected_revision=1)
        db.commit()
    async def unavailable(message):
        raise AssertionError('Restoring an unpublished setting must not need the target')
    monkeypatch.setattr(folder_channel, 'request', unavailable)
    response = await api.post('/api/security/changes/preview', headers=headers, json={
        'scope': scope, 'revision': 2, 'overrides': {'allowed_folders': False}})
    assert response.status_code == 200, response.text
    preview = response.json()
    response = await api.post(f"/api/security/changes/{preview['id']}/apply", headers=headers, json={'digest': preview['digest']})
    assert response.status_code == 200, response.text
    with api.app.state.db() as db:
        assert db.query(SecurityFolderPolicy).one().state == 'applied'
        assert policy.folder_revision(db, 'local', account) == 2


async def test_w3_imported_off_without_folder_row_can_restore_on(api, project):
    from acc.server import security_settings as policy
    from acc.server.models import SecurityFolderPolicy, SecurityPolicy
    with api.app.state.db() as db:
        scope = policy.account_scope(db, 'local', project['account'])
        db.add(SecurityPolicy(key=policy.scope_key(scope), scope=scope, overrides={'allowed_folders': False}, registry_version=1))
        db.commit()
        policy.save(db, scope, {'allowed_folders': True}, actor='owner', expected_revision=0)
        db.commit()
        assert db.query(SecurityFolderPolicy).count() == 0
        policy.folder_revision(db, 'local', project['account'])


async def test_w4_inventory_keeps_stored_state_separate_from_desired_mismatch(api, project):
    from acc.server import security_settings as policy
    headers = await verified(api)
    with api.app.state.db() as db:
        policy.save(db, {'kind': 'account', 'machine': 'local', 'account': project['account']},
            {'allowed_folders': False}, actor='owner', expected_revision=0)
        from acc.server.models import SecurityFolderPolicy
        row = db.query(SecurityFolderPolicy).one()
        row.detail = {**row.detail, 'allowed_folders': True, 'recovered_from': 'a' * 32}
        db.commit()
    response = await api.get('/api/security/folders', headers=headers)
    row = next(row for row in response.json()['accounts'] if row['scope']['account'] == project['account'])
    assert row['state'] == 'pending'
    assert row['stored_state'] == 'pending_policy'
    assert row['allowed_folders'] is False and row['detail']['allowed_folders'] is True
    assert row['retry_available'] is False


async def test_w5_switch_preview_closes_db_before_target_and_rechecks_revision(api, project, monkeypatch):
    from contextlib import contextmanager

    from acc import folder_channel
    from acc.server.models import SecurityRevision
    headers = await verified(api)
    ctx = api.app.state.ctx
    original = ctx.db
    active = 0
    @contextmanager
    def tracked():
        nonlocal active
        with original() as db:
            active += 1
            try:
                yield db
            finally:
                active -= 1
    monkeypatch.setattr(ctx, 'db', tracked)
    async def target(message):
        assert active == 0, 'idle DB transaction held during target await'
        with original() as db:
            db.merge(SecurityRevision(id=1, revision=1))
            db.commit()
        return {'policy_qualified': True, 'policy_recovery': True}
    monkeypatch.setattr(folder_channel, 'request', target)
    response = await api.post('/api/security/changes/preview', headers=headers, json={
        'scope': {'kind': 'account', 'machine': 'local', 'account': project['account']},
        'revision': 0, 'overrides': {'allowed_folders': False}})
    assert response.status_code == 409
    assert 'changed' in response.json()['detail']


@pytest.mark.parametrize('change', ['deleted', 'mapping'])
async def test_w1_owner_recovers_started_unfinishable_delivery(api, project, tmp_path, monkeypatch, change):
    import os
    import shutil

    from acc import folder_channel
    from acc.folder_policy import FolderError
    from acc.folder_worker import FolderWorker
    from acc.server.models import AuditLog, SecurityFolderPolicy
    headers = await verified(api)
    account = project['account']
    host = tmp_path / 'host'
    repo = host / 'home/alex/src/repo'
    repo.mkdir(parents=True)
    state = tmp_path / 'worker'
    state.mkdir(mode=0o700)
    roots = ['/home/alex-agent/workspaces']
    mapping = {'machine_id': 'local', 'qualified': False, 'policy_qualified': True, 'accounts': {account: {
        'uid': 12345, 'owner_uid': os.getuid(), 'homes': ['/home/alex'], 'protected': ['/srv/acc'], 'roots': roots}}}
    broker = {}
    def reload(_, payload):
        if broker and payload['revision'] <= broker['revision'] and payload != broker:
            raise FolderError('conflicting stale broker revision')
        broker.clear()
        broker.update(payload)
        if payload['revision'] == 1:
            raise OSError('acknowledgement lost after broker accepted revision')
        return {'revision': payload['revision']}
    with FolderWorker(state, mapping, host_root=host, owner=os.getuid(), reload=reload) as worker:
        async def target(message):
            return worker.handle(message)
        monkeypatch.setattr(folder_channel, 'request', target)
        preview = await api.post('/api/security/folders/preview', headers=headers, json={
            'machine': 'local', 'account': account, 'roots': roots + ['/home/alex/src/repo'], 'revision': 0})
        assert preview.status_code == 200, preview.text
        data = preview.json()
        published = await api.post(f"/api/security/folders/{data['id']}/apply", headers=headers, json={'digest': data['digest']})
        assert published.status_code == 503
        from acc.server.models import SecurityChange
        with api.app.state.db() as db:
            approval = db.get(SecurityChange, data['id'])
            approval.actor, approval.session_hash = 'former-owner', 'other-sign-in'
            db.commit()
        if change == 'deleted':
            shutil.rmtree(repo)
        else:
            worker.policy['accounts'][account]['homes'].append('/home/blair')
        review = await api.get(f"/api/security/folders/operations/{data['id']}", headers=headers)
        assert review.status_code == 200, review.text
        recovery = review.json()['recovery']
        assert review.json()['retry_available'] is False
        assert recovery['roots'] == roots and recovery['allowed_folders'] is True
        refused = await api.post(f"/api/security/folders/{data['id']}/recover", headers=headers,
            json={'digest': data['digest'], 'recovery_digest': '0' * 64})
        assert refused.status_code == 409
        with api.app.state.db() as db:
            assert db.query(SecurityFolderPolicy).one().state == 'recovering'
        response = await api.post(f"/api/security/folders/{data['id']}/recover", headers=headers,
            json={'digest': data['digest'], 'recovery_digest': recovery['digest']})
        assert response.status_code == 200, response.text
        assert response.json()['state'] == 'pending_policy'
        with api.app.state.db() as db:
            row = db.query(SecurityFolderPolicy).one()
            assert row.state == 'pending_policy'
            assert row.revision == row.applied_revision == 2
            assert row.roots == roots and row.detail['allowed_folders'] is True
            assert db.query(AuditLog).filter_by(action='security.folders.recovered', target=data['id']).one()
        assert broker['revision'] == 2 and broker['roots'] == roots
        with pytest.raises(FolderError):
            worker.handle({'action': 'policy_apply', 'operation_id': data['id'], 'machine_id': 'local',
                'account': account, 'revision': 0, 'digest': data['digest']})
        # An owner can now restore the acknowledged setting without needing the target.
        scope = {'kind': 'account', 'machine': 'local', 'account': account}
        revision = (await api.get('/api/security/scopes', headers=headers)).json()['revision']
        cancel = await api.post('/api/security/changes/preview', headers=headers,
            json={'scope': scope, 'revision': revision, 'overrides': {'allowed_folders': True}})
        assert cancel.status_code == 200, cancel.text
        cancel = cancel.json()
        assert (await api.post(f"/api/security/changes/{cancel['id']}/apply", headers=headers,
            json={'digest': cancel['digest']})).status_code == 200


async def test_w1_old_worker_cannot_accept_new_folder_preview(api, monkeypatch):
    from acc import folder_channel
    headers = await verified(api)
    async def old_worker(message):
        if message['action'] == 'status':
            return {'policy_qualified': True, 'revision': 0, 'roots': ['/home/alex-agent/workspaces'], 'allowed_folders': True}
        return {'digest': 'a' * 64, 'roots': message['roots'], 'legacy_roots': message['roots']}
    monkeypatch.setattr(folder_channel, 'request', old_worker)
    result = await api.post('/api/security/folders/preview', headers=headers, json={
        'machine': 'local', 'account': api.app.state.ctx.settings.default_account,
        'roots': ['/home/alex-agent/workspaces'], 'revision': 0})
    assert result.status_code == 409
    assert 'update' in result.json()['detail'].lower()


@pytest.mark.parametrize('delivery', ['apply', 'retry', 'switch'])
@pytest.mark.parametrize('first_reply', ['apply', 'recover', 'older_recovery'])
async def test_x1_recovery_wins_over_transport_held_delivery_replies(api, project, tmp_path, monkeypatch, delivery, first_reply):
    import asyncio
    import os

    from acc import folder_channel
    from acc.folder_worker import FolderWorker
    from acc.server import security_settings as policy
    from acc.server.models import SecurityChange, SecurityFolderPolicy
    headers = await verified(api)
    account = project['account']
    roots = ['/home/alex-agent/workspaces']
    state = tmp_path / 'worker'
    state.mkdir(mode=0o700)
    mapping = {'machine_id': 'local', 'qualified': False, 'policy_qualified': True, 'accounts': {account: {
        'uid': 12345, 'owner_uid': os.getuid(), 'homes': ['/home/alex'], 'protected': ['/srv/acc'], 'roots': roots}}}
    apply_ready, recover_ready = asyncio.Event(), asyncio.Event()
    apply_release, recover_release = asyncio.Event(), asyncio.Event()
    newer_ready, newer_release = asyncio.Event(), asyncio.Event()
    recover_calls = 0
    fail_first = delivery == 'retry'
    with FolderWorker(state, mapping, host_root=tmp_path, owner=os.getuid(),
                      reload=lambda _, value: {'revision': value['revision']}) as worker:
        async def target(message):
            nonlocal fail_first, recover_calls
            if message['action'] == 'policy_apply' and fail_first:
                fail_first = False
                raise OSError('initial delivery lost')
            result = worker.handle(message)
            if message['action'] == 'policy_apply':
                apply_ready.set()
                await apply_release.wait()
            elif message['action'] == 'policy_recover':
                recover_calls += 1
                if recover_calls == 1:
                    recover_ready.set()
                    await recover_release.wait()
                else:
                    newer_ready.set()
                    await newer_release.wait()
            return result
        monkeypatch.setattr(folder_channel, 'request', target)
        if delivery == 'switch':
            reviewed = await api.post('/api/security/changes/preview', headers=headers, json={
                'scope': {'kind': 'account', 'machine': 'local', 'account': account},
                'revision': 0, 'overrides': {'allowed_folders': False}})
        else:
            reviewed = await api.post('/api/security/folders/preview', headers=headers, json={
                'machine': 'local', 'account': account, 'roots': roots, 'revision': 0})
        assert reviewed.status_code == 200, reviewed.text
        data = reviewed.json()
        base = '/api/security/changes' if delivery == 'switch' else '/api/security/folders'
        if delivery == 'retry':
            failed = await api.post(f"{base}/{data['id']}/apply", headers=headers, json={'digest': data['digest']})
            assert failed.status_code == 503
        route = 'retry' if delivery == 'retry' else 'apply'
        applying = asyncio.create_task(api.post(f"{base}/{data['id']}/{route}", headers=headers, json={'digest': data['digest']}))
        recovering = newer = None
        try:
            await asyncio.wait_for(apply_ready.wait(), 10)
            with api.app.state.db() as db:
                row = db.query(SecurityFolderPolicy).one()
                operation = row.detail['operation_id']
                digest = db.get(SecurityChange, operation).digest
            review = await api.get(f'/api/security/folders/operations/{operation}', headers=headers)
            assert review.status_code == 200, review.text
            assert review.json()['recovery']['revision'] == 1
            recovering = asyncio.create_task(api.post(f'/api/security/folders/{operation}/recover', headers=headers,
                json={'digest': digest, 'recovery_digest': review.json()['recovery']['digest']}))
            await asyncio.wait_for(recover_ready.wait(), 10)
            if first_reply == 'older_recovery':
                fresh = await api.get(f'/api/security/folders/operations/{operation}', headers=headers)
                assert fresh.status_code == 200, fresh.text
                newer = asyncio.create_task(api.post(f'/api/security/folders/{operation}/recover', headers=headers,
                    json={'digest': digest, 'recovery_digest': fresh.json()['recovery']['digest']}))
                await asyncio.wait_for(newer_ready.wait(), 10)
                recover_release.set()
                older = await recovering
                assert older.status_code == 409, older.text
                newer_release.set()
                recovered = await newer
                apply_release.set()
                applied = await applying
            elif first_reply == 'apply':
                apply_release.set()
                applied = await applying
                recover_release.set()
                recovered = await recovering
            else:
                recover_release.set()
                recovered = await recovering
                apply_release.set()
                applied = await applying
            assert recovered.status_code == 200, recovered.text
            if delivery == 'switch':
                assert applied.status_code == 200, applied.text
                assert applied.json()['folder_application']['state'] == 'pending'
            else:
                assert applied.status_code == 409, applied.text
            with api.app.state.db() as db:
                row = db.query(SecurityFolderPolicy).one()
                assert (row.state, row.revision, row.applied_revision) == (('pending_policy', 3, 3) if first_reply == 'older_recovery' else ('pending_policy', 2, 2))
                assert 'operation_id' not in row.detail
                # An unchanged, reviewed save can release this recovered fence.
                policy.save(db, policy.account_scope(db, 'local', account),
                    {'allowed_folders': delivery != 'switch'}, actor='owner', expected_revision=policy.revision(db))
                db.commit()
                assert policy.folder_revision(db, 'local', account) == (3 if first_reply == 'older_recovery' else 2)
        finally:
            apply_release.set()
            recover_release.set()
            newer_release.set()
            await asyncio.gather(applying, *[task for task in (recovering, newer) if task], return_exceptions=True)


@pytest.mark.parametrize('stored_state', ['pending_policy', 'pending', 'recovering'])
async def test_x3_only_acknowledged_recovery_offers_unchanged_confirmation(api, project, monkeypatch, stored_state):
    from acc import folder_channel
    from acc.server import security_settings as policy
    from acc.server.models import SecurityFolderPolicy
    headers = await verified(api)
    account = project['account']
    with api.app.state.db() as db:
        scope = policy.account_scope(db, 'local', account)
        db.add(SecurityFolderPolicy(key=policy.scope_key(scope), machine_id='local', account=account,
            roots=['/home/alex-agent/workspaces'], state=stored_state, revision=2, applied_revision=2,
            detail={'allowed_folders': True, 'recovered_from': 'a' * 32}))
        db.commit()
    response = await api.get('/api/security/effective', headers=headers, params={'machine': 'local', 'account': account})
    assert response.status_code == 200, response.text
    assert response.json()['can_confirm_folder_recovery'] is (stored_state == 'pending_policy')
    if stored_state != 'pending_policy':
        return
    async def no_reload(message):
        pytest.fail('Confirming an acknowledged recovery must not reload the target')
    monkeypatch.setattr(folder_channel, 'request', no_reload)
    preview = await api.post('/api/security/changes/preview', headers=headers, json={'scope': {
        'kind': 'account', 'machine': 'local', 'account': account}, 'revision': 0, 'overrides': {}})
    assert preview.status_code == 200, preview.text
    data = preview.json()
    assert data['before'] == data['after'] == {}
    applied = await api.post(f"/api/security/changes/{data['id']}/apply", headers=headers, json={'digest': data['digest']})
    assert applied.status_code == 200, applied.text
    with api.app.state.db() as db:
        assert policy.folder_revision(db, 'local', account) == 2
