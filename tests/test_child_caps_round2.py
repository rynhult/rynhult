"""Round 2: shared approvals, non-reviving transfer and canonical launch evidence."""
import hashlib
import json
from types import SimpleNamespace

import pytest
from helpers import write_db
from sqlalchemy import select
from test_session_capabilities import family

from acc.server import approvals, child_caps, session_caps
from acc.server.compiler import LaunchInputs, launch_digest
from acc.server.models import ApprovalBatch, AuditLog, LaunchSpec, Session, ToolRequest
from acc.server.tools import ToolRefused


@pytest.mark.parametrize('owner,root_owner', [(None, None), (None, '@api'), ('@api', None)])
def test_chain_refuses_missing_recorded_owner(owner, root_owner):
    session = SimpleNamespace(id='recorded', parent_session_id=None, parent_chain=[], owner=owner, root_owner=root_owner)
    with pytest.raises(child_caps.Refused, match='missing recorded owner'):
        child_caps.chain(None, session)


@pytest.mark.parametrize('stale_reason', ['narrowed', 'launch_changed', 'missing', 'malformed'])
async def test_transfer_preserves_stale_fences_and_spec_identity(api, project, stale_reason):
    root, stale = family(api, project, {})
    with write_db(api.app.state.db) as db:
        parent, child = db.get(Session, root), db.get(Session, stale)
        if stale_reason == 'narrowed':
            session_caps.narrow(db, parent, {'models': ['claude:sonnet']}, '@api')
        elif stale_reason == 'launch_changed':
            parent.launch_spec.digest = 'new-parent-generation'
        else:
            child.launch_spec.files = {} if stale_reason == 'missing' else {child_caps.FILE: 'invalid-json'}
        healthy = Session(id='healthy', project=parent.project, provider='claude', name='Healthy', tmux_name='healthy',
                          owner='@api', run_as=parent.run_as, desired_state='running', parent_session_id=root,
                          launch_spec=LaunchSpec(provider='claude', inputs=LaunchInputs(provider='claude', model='sonnet').as_dict(),
                                                 files={child_caps.FILE: json.dumps(child_caps.proof(db, parent))}, digest='healthy-before'))
        healthy.launch_spec.digest = launch_digest(healthy.launch_spec.inputs, healthy.launch_spec.files)
        healthy_digest_before = healthy.launch_spec.digest
        session_caps.attach(db, healthy)
        db.add(healthy)
        db.flush()
        session_caps.check_execution(db, healthy)
        old_specs = set(db.scalars(select(LaunchSpec.id)))
        root_id, root_digest = parent.launch_spec.id, parent.launch_spec.digest
        stale_id, stale_digest, stale_files = child.launch_spec.id, child.launch_spec.digest, dict(child.launch_spec.files)
        healthy_id = healthy.launch_spec.id
        child_caps.transfer_owner(db, [healthy, child, parent], 'new-owner', '@api')
        db.commit()
        assert parent.launch_spec.id == root_id and parent.launch_spec.digest == root_digest
        assert child.launch_spec.id == stale_id and child.launch_spec.digest == stale_digest
        assert child.launch_spec.files == stale_files
        assert healthy.launch_spec.id == healthy_id
        assert set(db.scalars(select(LaunchSpec.id))) == old_specs
        expected = hashlib.sha256(json.dumps({'inputs': healthy.launch_spec.inputs,
                                              'files': healthy.launch_spec.files}, sort_keys=True).encode()).hexdigest()
        assert healthy.launch_spec.digest == expected
        session_caps.check_execution(db, healthy)
        with pytest.raises((child_caps.Refused, ValueError)):
            child_caps.check(db, child)
        audit = db.scalar(select(AuditLog).where(AuditLog.action == 'session.owner.transfer'))
        assert audit.detail['preserved_stale_proofs'] == [stale]
        assert audit.detail['refreshed_proofs'] == ['healthy']
        assert audit.detail['launch_digest_changes'] == [{'session': 'healthy', 'before': healthy_digest_before, 'after': expected, 'scheme': 'compiler_inputs_files'}]
        assert audit.detail['consent_review_required'] is True


async def test_transfer_of_shared_spec_does_not_orphan_or_mutate_other_session(api, project):
    root, child = family(api, project, {})
    with write_db(api.app.state.db) as db:
        worker = db.get(Session, child)
        sibling = Session(id='sibling', project=worker.project, provider='claude', name='Sibling', tmux_name='sibling',
                          owner='@api', run_as=worker.run_as, desired_state='running', parent_session_id=root,
                          launch_spec=worker.launch_spec)
        session_caps.attach(db, sibling)
        db.add(sibling)
        db.flush()
        original = worker.launch_spec
        child_caps.transfer_owner(db, [db.get(Session, root), worker, sibling], 'new-owner', '@api')
        db.commit()
        assert worker.launch_spec.id != sibling.launch_spec.id
        assert original.id in {worker.launch_spec.id, sibling.launch_spec.id}
        referenced = set(db.scalars(select(Session.launch_spec_id)))
        assert set(db.scalars(select(LaunchSpec.id))) == referenced
        child_caps.check(db, worker)
        child_caps.check(db, sibling)


async def test_batch_root_person_check_at_preview_and_execution(api, project, env, member_accounts, monkeypatch):
    from test_auth import cookie_client, do_setup
    from test_users import browser, csrf, join
    await do_setup(api, env)
    root, child = family(api, project, {})
    async with browser(api) as member:
        link = (await api.post('/api/users/invites', json={'role': 'member', 'account': 'dana-agent'})).json()['link']
        assert (await join(member, link, 'dana')).status_code == 200
        with write_db(api.app.state.db) as db:
            rows = [db.get(Session, root), db.get(Session, child)]
            for row in rows:
                row.run_as = 'dana-agent'
            rows[1].launch_spec.files = {child_caps.FILE: json.dumps(child_caps.proof(db, rows[0]))}
            rows[1].launch_spec.digest = launch_digest(rows[1].launch_spec.inputs, rows[1].launch_spec.files)
            child_caps.transfer_owner(db, rows, 'dana', '@api')
            db.add(ToolRequest(id='child-batch', session_id=child, project_id=project['id'], owner='dana',
                               account='dana-agent', host='local', manager='npm', name='prettier',
                               reason='format', operation='install'))
            db.commit()
        owner = cookie_client(api)
        response = await owner.post('/api/approvals/batches/preview', json={'requests': ['child-batch']}, headers=csrf(owner))
        assert response.status_code == 403, response.text
        # A historical/forged owner preview must not bypass the check at use.
        with write_db(api.app.state.db) as db:
            payload = [{'request_id': 'child-batch', 'digest': approvals.digest(approvals.tool_payload(db.get(ToolRequest, 'child-batch')))}]
            from datetime import timedelta

            from acc.server import security_settings
            from acc.server.models import utcnow
            db.add(ApprovalBatch(id='old-owner-preview', actor='casey', payload=payload, digest=approvals.digest(payload),
                                 security_revision=security_settings.revision(db), expires_at=utcnow() + timedelta(minutes=5)))
            db.commit()
        response = await owner.post('/api/approvals/batches/old-owner-preview/approve',
                                    json={'digest': approvals.digest(payload)}, headers=csrf(owner))
        assert response.status_code == 403, response.text
        with write_db(api.app.state.db) as db:
            for actor, browser_session in [('@api', True), ('session:' + root, True), ('dana', False)]:
                with pytest.raises((ToolRefused, approvals.Refused)):
                    approvals.preview_batch(db, actor, ['child-batch'], browser_session=browser_session)
            assert db.get(ToolRequest, 'child-batch').status == 'pending_approval'
            assert not db.get(ApprovalBatch, 'old-owner-preview').used
        response = await member.post('/api/approvals/batches/preview', json={'requests': ['child-batch']}, headers=csrf(member))
        assert response.status_code == 201, response.text
        batch = response.json()
        with write_db(api.app.state.db) as db:
            with pytest.raises(ToolRefused):
                approvals.approve_batch(db, batch['id'], 'dana', batch['digest'])  # no browser
        monkeypatch.setattr(api.app.state.supervisor.tools, 'schedule', lambda *a: None)
        response = await member.post('/api/approvals/batches/' + batch['id'] + '/approve',
                                     json={'digest': batch['digest']}, headers=csrf(member))
        assert response.status_code == 200, response.text
        with api.app.state.db() as db:
            assert db.get(ToolRequest, 'child-batch').decided_by == 'dana'


@pytest.mark.parametrize('scheme', ['compiler_inputs_files', 'files_json', 'files_compact_json', 'unknown'])
async def test_transfer_retains_existing_digest_scheme(api, project, scheme):
    root, child = family(api, project, {})
    formulas = {
        'compiler_inputs_files': lambda spec, files: launch_digest(spec.inputs, files),
        'files_json': lambda spec, files: hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest(),
        'files_compact_json': lambda spec, files: child_caps.digest(files),
        'unknown': lambda spec, files: 'opaque-legacy-digest',
    }
    with write_db(api.app.state.db) as db:
        worker = db.get(Session, child)
        formula = formulas[scheme]
        worker.launch_spec.digest = formula(worker.launch_spec, worker.launch_spec.files)
        before = dict(worker.launch_spec.files)
        child_caps.transfer_owner(db, [db.get(Session, root), worker], 'new-owner', '@api')
        db.commit()
        assert worker.launch_spec.digest == formula(worker.launch_spec, worker.launch_spec.files)
        if scheme == 'unknown':
            assert worker.launch_spec.files == before
            with pytest.raises(child_caps.Refused, match='parent grant changed'):
                child_caps.check(db, worker)
        else:
            child_caps.check(db, worker)
            audit = db.scalar(select(AuditLog).where(AuditLog.action == 'session.owner.transfer'))
            assert audit.detail['launch_digest_changes'][0]['scheme'] == scheme


@pytest.mark.parametrize('mode,allowed', [('default', False), ('acceptEdits', True)])
async def test_claude_lead_caps_codex_workspace_write_helper(api, project, env, mode, allowed):
    from test_changes import repo
    from test_team import grant, request
    repo(env['workspaces'] / 'demo')
    response = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude',
        'name': 'Asking lead', 'permission_mode': mode, 'worktree': True,
        'orchestrator': grant(agents=[{'agent': 'codex', 'profile': None}])})
    assert response.status_code == 201, response.text
    root = response.json()
    result = await request(api, env, root, 'start', name='Codex helper', agent='codex', profile=None, task='work')
    if not allowed:
        assert not result['ok'], result
        assert f"limited by {root['id']}" in str(result)
        assert 'child requires edits without asking (level 2); parent permits asks (level 1)' in str(result)
        with api.app.state.db() as db:
            assert db.scalar(select(Session.id).where(Session.parent_session_id == root['id'])) is None
        return
    assert result['ok'], result
    with api.app.state.db() as db:
        worker = db.get(Session, result['result']['id'])
        assert worker.parent_session_id == root['id']
        assert worker.launch_spec.inputs['approval'] == 'on-request'
        assert worker.launch_spec.inputs['sandbox'] == 'workspace-write'
        session_caps.check_execution(db, worker)


async def test_unsupported_opencode_explanation_is_not_offered(api, project, env, monkeypatch):
    from test_explanations import source
    source_session = await source(api, project, env)

    original = api.app.state.supervisor.broker.call

    async def call(operation, **kwargs):
        if operation == 'auth_health':
            return {'opencode': {'logged_in': True}, 'codex': {'logged_in': True}}
        return await original(operation, **kwargs)

    monkeypatch.setattr(api.app.state.supervisor.broker, 'call', call)
    path = f"/api/sessions/{source_session['id']}/explanation"
    response = await api.get(path + '/agents')
    assert response.status_code == 200, response.text
    assert 'opencode' not in {agent['id'] for agent in response.json()['agents']}
    assert 'codex' in {agent['id'] for agent in response.json()['agents']}
    assert response.json()['unavailable'] == [{'id': 'opencode', 'label': 'OpenCode',
                                             'reason': 'Read-only explanation runner is not supported.'}]
    response = await api.post(path, json={'provider': 'opencode', 'model': 'opencode/fake-free'})
    assert response.status_code == 422


async def test_root_only_transfer_keeps_launch_spec_and_records_consent_review(api, project):
    from test_supervisor import launch
    response = await launch(api, project)
    with write_db(api.app.state.db) as db:
        root = db.get(Session, response['id'])
        spec_id, digest = root.launch_spec.id, root.launch_spec.digest
        child_caps.transfer_owner(db, [root], 'new-owner', '@api')
        db.commit()
        assert root.launch_spec.id == spec_id and root.launch_spec.digest == digest
        audit = db.scalar(select(AuditLog).where(AuditLog.action == 'session.owner.transfer'))
        assert audit.detail['launch_digest_changes'] == []
        assert audit.detail['consent_review_required'] is True  # owner binding changed even without new launch bytes
