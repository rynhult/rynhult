"""Owner consent → root protocol → durable admission fence, never live services."""
import os

import pytest
from folder_host_fixture import FixtureHost
from test_security_folders import verified

from acc.folder_worker import FolderWorker
from acc.server.models import AuditLog, SecurityChange


@pytest.fixture
async def access(api, tmp_path, monkeypatch):
    from acc import folder_channel
    headers = await verified(api)
    account = api.app.state.ctx.settings.default_account
    host = tmp_path / 'host'
    repo = host / 'home/alex/src/repo'
    repo.mkdir(parents=True)
    (repo / 'data').write_text('unchanged')
    (host / 'state').mkdir(mode=0o700)
    mapping = {'machine_id': 'local', 'qualified': True, 'accounts': {account: {
        'uid': 12345, 'owner_uid': os.getuid(), 'homes': ['/home/alex'], 'protected': ['/srv/acc'],
        'roots': ['/home/alex-agent/workspaces']}}}
    lifecycle = FixtureHost()
    with FolderWorker(host / 'state', mapping, host_root=host, owner=os.getuid(), lifecycle=lifecycle) as worker:
        async def target(message):
            if message['action'] in ('apply', 'undo'):
                from acc.server import security_settings
                from acc.server.broker_client import BrokerError
                with api.app.state.db() as db:
                    with pytest.raises(BrokerError, match='access'):
                        security_settings.folder_revision(db, 'local', account)
            return worker.handle(message)
        monkeypatch.setattr(folder_channel, 'request', target)
        yield api, headers, account, worker, repo


async def preview(access):
    api, headers, account, _, _ = access
    revision = (await api.get('/api/security/scopes', headers=headers)).json()['revision']
    response = await api.post('/api/security/access/preview', headers=headers, json={
        'machine': 'local', 'account': account, 'path': '/home/alex/src/repo',
        'method': 'named_acl', 'write': False, 'revision': revision})
    assert response.status_code == 200, response.text
    return response.json()


async def test_owner_review_apply_and_separately_reviewed_undo(access):
    api, headers, account, worker, repo = access
    before = (repo / 'data').stat().st_mode
    review = await preview(access)
    assert review['preview']['changes']
    assert '.env' in review['warning'] and 'copied' in review['warning']
    assert worker.read('access-' + account + '.json') is None
    applied = await api.post(f"/api/security/access/{review['id']}/apply", headers=headers, json={'digest': review['digest']})
    assert applied.status_code == 200, applied.text
    assert applied.json()['state'] == 'applied'
    replay = await api.post(f"/api/security/access/{review['id']}/apply", headers=headers, json={'digest': review['digest']})
    assert replay.status_code == 409
    response = await api.post(f"/api/security/access/{review['id']}/undo-preview", headers=headers, json={})
    assert response.status_code == 200, response.text
    undo = response.json()
    response = await api.post(f"/api/security/access/{undo['id']}/apply", headers=headers, json={'digest': undo['digest']})
    assert response.status_code == 200, response.text
    assert response.json()['state'] == 'undone'
    assert (repo / 'data').stat().st_mode == before
    assert (repo / 'data').read_text() == 'unchanged'
    with api.app.state.db() as db:
        actions = [a.action for a in db.query(AuditLog).filter(AuditLog.action.like('security.access.%'))]
        assert 'security.access.preview' in actions and 'security.access.applied' in actions and 'security.access.undone' in actions


async def test_gate_blocks_grant_before_root_mutation(access, project):
    from acc.server.orchestrator.gates import GateJob
    api, headers, account, worker, _ = access
    review = await preview(access)
    with api.app.state.db() as db:
        db.add(GateJob(id='f' * 32, project_id=project['id'], branch='main', suite='related', priority='check',
            state='queued', host='local', account=account, requested_by='casey'))
        db.commit()
    response = await api.post(f"/api/security/access/{review['id']}/apply", headers=headers, json={'digest': review['digest']})
    assert response.status_code == 409
    assert worker.read('access-' + account + '.json') is None


async def test_lost_reply_remains_pending_then_refresh_recovers_actual_result(access, monkeypatch):
    from acc import folder_channel
    api, headers, account, worker, _ = access
    review = await preview(access)
    original = folder_channel.request
    async def lost(message):
        reply = await original(message)
        if message['action'] == 'apply':
            raise OSError('reply lost after mutation')
        return reply
    monkeypatch.setattr(folder_channel, 'request', lost)
    response = await api.post(f"/api/security/access/{review['id']}/apply", headers=headers, json={'digest': review['digest']})
    assert response.status_code == 503
    assert (await api.get(f"/api/security/changes/{review['id']}", headers=headers)).status_code == 404
    with api.app.state.db() as db:
        change = db.get(SecurityChange, review['id'])
        assert change.payload['access_state'] == 'pending'
    response = await api.post(f"/api/security/access/{review['id']}/refresh", headers=headers, json={})
    assert response.status_code == 200, response.text
    assert response.json()['state'] == 'applied'


async def test_access_installation_tokens_cannot_even_preview(api):
    response = await api.post('/api/security/access/preview', json={
        'machine': 'local', 'account': 'agent', 'path': '/home/alex/src/repo',
        'method': 'named_acl', 'write': False, 'revision': 0})
    assert response.status_code == 403


async def test_unstarted_disconnected_grant_can_be_cancelled_without_weakening_fence(access, monkeypatch):
    from acc import folder_channel
    from acc.server import security_settings
    api, headers, account, worker, _ = access
    review = await preview(access)
    original = folder_channel.request
    async def offline(message):
        if message['action'] == 'apply':
            raise OSError('never dispatched')
        return await original(message)
    monkeypatch.setattr(folder_channel, 'request', offline)
    response = await api.post(f"/api/security/access/{review['id']}/apply", headers=headers, json={'digest': review['digest']})
    assert response.status_code == 503
    response = await api.post(f"/api/security/access/{review['id']}/cancel", headers=headers, json={})
    assert response.status_code == 200, response.text
    assert response.json()['state'] == 'cancelled'
    with api.app.state.db() as db:
        assert security_settings.folder_revision(db, 'local', account) is None
    assert worker.read('access-' + account + '.json') is None


async def test_fresh_owner_signin_can_recover_approved_grant_but_not_use_old_preview(access):
    api, headers, _, _, _ = access
    review = await preview(access)
    with api.app.state.db() as db:
        row = db.get(SecurityChange, review['id'])
        row.session_hash = 'prior-signin'
        db.commit()
    response = await api.post(f"/api/security/access/{review['id']}/apply", headers=headers, json={'digest': review['digest']})
    assert response.status_code == 404
    # Only an already approved operation can be recovered across sign-ins.
    with api.app.state.db() as db:
        row = db.get(SecurityChange, review['id'])
        row.applied = True
        row.payload = {**row.payload, 'access_state': 'pending'}
        db.commit()
    response = await api.post(f"/api/security/access/{review['id']}/cancel", headers=headers, json={})
    assert response.status_code == 200, response.text


async def test_unstarted_undo_refresh_does_not_mistake_the_prior_grant_for_success(access, monkeypatch):
    from acc import folder_channel
    api, headers, _, _, _ = access
    review = await preview(access)
    assert (await api.post(f"/api/security/access/{review['id']}/apply", headers=headers, json={'digest':review['digest']})).status_code == 200
    response = await api.post(f"/api/security/access/{review['id']}/undo-preview", headers=headers, json={})
    undo = response.json()
    original = folder_channel.request
    async def lost(message):
        if message['action'] == 'undo':
            raise OSError('undo not dispatched')
        return await original(message)
    monkeypatch.setattr(folder_channel, 'request', lost)
    assert (await api.post(f"/api/security/access/{undo['id']}/apply", headers=headers, json={'digest':undo['digest']})).status_code == 503
    response = await api.post(f"/api/security/access/{undo['id']}/refresh", headers=headers, json={})
    assert response.status_code == 200, response.text
    assert response.json()['state'] == 'pending'
    with api.app.state.db() as db:
        assert db.get(SecurityChange, undo['id']).payload['access_state'] == 'pending'


async def test_t1_active_account_session_blocks_filesystem_grant(access, project):
    from test_supervisor import launch
    api, headers, account, worker, repo = access
    session = await launch(api, project)
    review = await preview(access)
    refused = await api.post(f"/api/security/access/{review['id']}/apply", headers=headers,
                             json={'digest': review['digest']})
    assert refused.status_code == 409, refused.text
    assert 'Stop affected sessions' in refused.text
    assert worker.read('access-' + account + '.json') is None
    assert (await api.post(f"/api/sessions/{session['id']}/stop")).status_code == 200
