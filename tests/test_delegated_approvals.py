"""Authority changes must be scoped, attributable and fail closed at execution."""
from datetime import timedelta

import pytest
from helpers import write_db
from migration_revisions import DELEGATED_APPROVALS
from sqlalchemy import select
from test_tools import as_member, seed

from acc.server.models import AuditLog, Session, Team, ToolRequest, User, utcnow


async def world(api, project):
    sid = await seed(api, project)
    with write_db(api.app.state.db) as db:
        from acc.server.models import Setting
        db.merge(Setting(key='approvals.enabled', value=True))
        p = db.get(Session, sid).project
        p.owner, p.account, p.trusted = 'alex', 'alex-agent', True
        lead = db.get(Session, sid)
        lead.placement.account = 'alex-agent'
        lead.team_id = sid
        db.add(Team(id=sid, limits={'machines': ['local'], 'agents': [], 'total': 3},
                    expires_at=utcnow() + timedelta(hours=1)))
        db.commit()
    return sid


def issue(db, sid, **changes):
    from acc.server import approvals
    return approvals.grant(db, actor='alex', project_id=db.get(Session, sid).project_id,
        team_id=sid, action='tool.install', constraints={'manager': 'npm', 'name': 'prettier',
        'version': '3.6.2', 'registry': 'https://registry.npmjs.org'}, duration='team', **changes)


async def test_consent_is_exact_and_revocation_beats_queued_claim(api, project):
    from acc.server import approvals
    sid = await world(api, project)
    service = api.app.state.supervisor.tools
    rid = service.request_session(sid, manager='npm', name='prettier', version='3.6.2', reason='Format')
    with write_db(api.app.state.db) as db:
        consent = issue(db, sid)
        result = approvals.evaluate_tool(db, db.get(ToolRequest, rid), sid)
        assert result['source'] == 'C' and result['consent_id'] == consent.id
        assert result['lead_id'] == sid
        claim = approvals.claim_tool(db, db.get(ToolRequest, rid), sid)
        approvals.revoke(db, consent.id, 'alex', consent.revision)
        db.commit()
    with write_db(api.app.state.db) as db:
        with pytest.raises(approvals.Refused, match='revoked'):
            approvals.consume(db, claim.id)
        assert db.get(ToolRequest, rid).status == 'pending_approval'


@pytest.mark.parametrize('change', ['version', 'project', 'account', 'security', 'stop', 'expired', 'role', 'generation', 'feature'])
async def test_scope_and_current_authority_are_rechecked(api, project, change):
    from acc.server import approvals
    from acc.server.models import SecurityRevision
    sid = await world(api, project)
    rid = api.app.state.supervisor.tools.request_session(sid, manager='npm', name='prettier', version='3.6.2', reason='Format')
    with write_db(api.app.state.db) as db:
        consent = issue(db, sid)
        row = db.get(ToolRequest, rid)
        if change == 'version': row.version = '4.0.0'
        if change == 'project': row.project_id = None
        if change == 'account': db.get(Session, sid).placement.account = 'other-agent'
        if change == 'security': db.get(SecurityRevision, 1).revision += 1
        if change == 'stop': db.get(Team, sid).stopped = True
        if change == 'expired': consent.expires_at = utcnow() - timedelta(seconds=1)
        if change == 'role': db.scalar(select(User).where(User.username == 'alex')).role = 'owner'
        if change == 'generation': db.get(Team, sid).lifetime_revision += 1
        if change == 'feature':
            from acc.server.models import Setting
            db.merge(Setting(key='admin_features', value={'choices': {'teams': False}, 'revision': 1}))
        db.flush()
        assert approvals.evaluate_tool(db, row, sid)['source'] == 'P'
        db.commit()


async def test_worker_cannot_decide_or_grant_and_system_removal_unknown_are_person_only(api, project):
    from acc.server import approvals
    sid = await world(api, project)
    with write_db(api.app.state.db) as db:
        issue(db, sid)
        for manager, operation in [('apt', 'install'), ('npm', 'remove'), ('brew', 'install')]:
            row = ToolRequest(id='probe', session_id=sid, project_id=project['id'], owner='alex',
                account='alex-agent', host='local', manager=manager, operation=operation, name='prettier', version='3.6.2')
            assert approvals.evaluate_tool(db, row, sid)['source'] == 'P'
        with pytest.raises(approvals.Refused):
            approvals.grant(db, actor='@api', project_id=project['id'], team_id=sid, action='tool.install',
                constraints={}, duration='team')
        db.commit()
    as_member(api)
    bad = await api.post('/api/approvals/consents', json={'project_id': project['id'], 'team_id': sid,
        'action': 'publication', 'constraints': {}, 'duration': 'project'})
    assert bad.status_code in (403, 422)


async def test_batch_preview_is_bound_atomic_and_deduplicates_requests(api, project, monkeypatch):
    from acc.server import approvals
    sid = await world(api, project)
    service = api.app.state.supervisor.tools
    rid = service.request_session(sid, manager='npm', name='prettier', reason='Format')
    for _ in range(100):
        assert service.request_session(sid, manager='npm', name='prettier', reason='Format') == rid
    other = service.request_session(sid, manager='npm', name='typescript', reason='Compile')
    with write_db(api.app.state.db) as db:
        preview = approvals.preview_batch(db, 'alex', [rid, other])
        assert len(preview.payload) == 2
        db.get(ToolRequest, other).version = '5.0.0'
        db.commit()
    with write_db(api.app.state.db) as db:
        with pytest.raises(approvals.Refused):
            approvals.approve_batch(db, preview.id, 'alex', preview.digest)
        assert db.get(ToolRequest, rid).status == 'pending_approval'
    with write_db(api.app.state.db) as db:
        preview = approvals.preview_batch(db, 'alex', [rid, other])
        approvals.approve_batch(db, preview.id, 'alex', preview.digest)
        db.commit()
    with api.app.state.db() as db:
        assert db.get(ToolRequest, rid).status == 'approved'
        assert db.get(ToolRequest, other).status == 'approved'
        assert list(db.scalars(select(AuditLog.action).where(AuditLog.action == 'approval.batch.use')))


async def test_main_consent_keeps_merge_and_push_separate_and_cannot_execute_without_verifier(api, project):
    from acc.server import approvals
    sid = await world(api, project)
    with write_db(api.app.state.db) as db:
        p = db.get(Session, sid).project
        p.remote_url = 'https://git.example.lan/demo/app.git'
        consent = approvals.grant(db, actor='alex', project_id=p.id, team_id=sid, action='main.push',
            constraints={'repository': p.workspace_path, 'remote': p.remote_url, 'ref': 'refs/heads/trunk',
                         'verification_policy': 'required-gate-v1'}, duration='once', request_digest='a' * 64)
        assert consent.action == 'main.push'
        result = approvals.evaluate_main(db, sid, 'main.merge', consent.constraints, 'a' * 64)
        assert result['source'] == 'P'
        assert approvals.evaluate_main(db, sid, 'main.push', consent.constraints, 'a' * 64)['source'] == 'C'
        with pytest.raises(approvals.Refused, match='qualified'):
            approvals.claim_main(db, sid, 'main.push', consent.constraints, 'a' * 64)
        db.commit()


async def test_group_projects_and_installation_api_token_do_not_mint_consent(api, project):
    from acc.server import approvals
    from acc.server.models import Group
    sid = await world(api, project)
    with write_db(api.app.state.db) as db:
        db.add(Group(id='demo-group', name='Demo group', created_by='alex'))
        db.flush()
        p = db.get(Session, sid).project
        p.owner, p.group_id = None, 'demo-group'
        with pytest.raises(approvals.Refused):
            issue(db, sid)
    response = await api.post('/api/approvals/consents', json={'project_id': project['id'], 'team_id': sid,
        'action': 'tool.install', 'duration': 'team', 'constraints': {}})
    assert response.status_code == 403


async def test_unqualified_execution_never_calls_broker_and_grant_has_no_side_effect(api, project):
    from acc.server import approvals
    sid = await world(api, project)
    service = api.app.state.supervisor.tools
    rid = service.request_session(sid, manager='npm', name='prettier', version='3.6.2', reason='Format')
    with write_db(api.app.state.db) as db:
        consent = issue(db, sid)
        op = approvals.claim_tool(db, db.get(ToolRequest, rid), sid)
        with pytest.raises(approvals.Refused, match='qualified'):
            approvals.consume(db, op.id)
        assert db.get(ToolRequest, rid).status == 'pending_approval'
        assert consent.last_used_at is None
        db.commit()


async def test_project_policy_issues_bounded_children_and_parent_revocation_fences_them(api, project):
    from acc.server import approvals
    sid = await world(api, project)
    with write_db(api.app.state.db) as db:
        policy = approvals.grant(db, actor='alex', project_id=project['id'], team_id=sid,
            action='tool.install', constraints={'manager': 'npm', 'name': 'prettier', 'version': '3.6.2',
            'registry': 'https://registry.npmjs.org'}, duration='project')
        child = approvals.bind(db, policy.id, sid, actor='alex')
        assert child.parent_id == policy.id and child.duration == 'team'
        assert child.expires_at == db.get(Team, sid).expires_at
        assert approvals.bind(db, policy.id, sid, actor='alex').id == child.id
        approvals.revoke(db, policy.id, 'alex', policy.revision)
        with pytest.raises(approvals.Refused):
            approvals.current(db, child)
        db.commit()


def test_upgrade_preserves_old_decisions_and_downgrade_refuses_retained_consent(tmp_path):
    from alembic import command
    from alembic.script import ScriptDirectory
    from sqlalchemy import text

    from acc.server.db import alembic_config, make_engine
    config = alembic_config(f'sqlite:///{tmp_path / "migration.db"}')
    previous = ScriptDirectory.from_config(config).get_revision(DELEGATED_APPROVALS).down_revision
    command.upgrade(config, previous)
    engine = make_engine(config.get_main_option('sqlalchemy.url'))
    # Simulate the actual old column layout (0030 uses today's metadata on fresh DBs).
    with engine.begin() as db:
        db.execute(text('ALTER TABLE tool_request DROP COLUMN decision'))
    command.upgrade(config, DELEGATED_APPROVALS)
    with engine.begin() as db:
        assert db.scalar(text('SELECT count(*) FROM approval_consent')) == 0
        assert db.scalar(text('SELECT count(*) FROM approval_operation')) == 0
        assert db.scalar(text('SELECT count(*) FROM approval_batch')) == 0
        db.execute(text("INSERT INTO approval_batch VALUES ('preview', 'alex', 'digest', '[]', 0, '2026-11-01', 0)"))
    with pytest.raises(RuntimeError, match='Delegated approval records'):
        command.downgrade(config, previous)
    with engine.connect() as db:
        assert db.scalar(text('SELECT count(*) FROM approval_batch')) == 1
    engine.dispose()


async def test_consent_http_requires_real_recent_person_and_revoke_needs_no_step_up(api, project):
    from acc.server import auth
    from acc.server.models import AuthSession
    sid = await world(api, project)
    body = {'project_id': project['id'], 'team_id': sid, 'action': 'tool.install', 'duration': 'team',
            'constraints': {'manager': 'npm', 'name': 'prettier', 'version': '3.6.2', 'registry': 'https://registry.npmjs.org'}}
    assert (await api.post('/api/approvals/consents', json=body)).status_code == 403
    with write_db(api.app.state.db) as db:
        user = db.scalar(select(User).where(User.username == 'alex'))
        token, csrf = auth.create_session(db, user, 'test')
        db.commit()
    api.headers.pop('authorization', None)
    api.cookies.set('acc_session', token)
    api.headers.update({'x-acc-csrf': csrf, 'origin': 'http://acc'})
    assert (await api.post('/api/approvals/consents', json=body)).status_code == 403
    with write_db(api.app.state.db) as db:
        db.get(AuthSession, auth.digest(token)).reauthenticated_at = auth.now()
        db.commit()
    response = await api.post('/api/approvals/consents', json=body)
    assert response.status_code == 201, response.text
    consent = response.json()
    assert 'verification_ref' not in consent
    assert (await api.get('/api/approvals/consents')).json()['execution_qualified'] is False
    with write_db(api.app.state.db) as db:
        db.get(AuthSession, auth.digest(token)).reauthenticated_at = None
        db.commit()
    response = await api.post(f"/api/approvals/consents/{consent['id']}/revoke", json={'revision': 1})
    assert response.status_code == 200, response.text
    assert response.json()['state'] == 'revoked'


async def test_batch_replay_other_person_and_expiry_are_refused(api, project):
    from acc.server import approvals
    sid = await world(api, project)
    service = api.app.state.supervisor.tools
    rid = service.request_session(sid, manager='npm', name='prettier', reason='Format')
    with write_db(api.app.state.db) as db:
        with pytest.raises(approvals.Refused):
            approvals.preview_batch(db, 'alex', [rid] * 51)
        preview = approvals.preview_batch(db, 'alex', [rid])
        with pytest.raises(approvals.Refused):
            approvals.approve_batch(db, preview.id, 'someone-else', preview.digest)
        preview.expires_at = utcnow() - timedelta(seconds=1)
        with pytest.raises(approvals.Refused):
            approvals.approve_batch(db, preview.id, 'alex', preview.digest)
        assert db.get(ToolRequest, rid).status == 'pending_approval'
        fresh = approvals.preview_batch(db, 'alex', [rid])
        approvals.approve_batch(db, fresh.id, 'alex', fresh.digest)
        with pytest.raises(approvals.Refused):
            approvals.approve_batch(db, fresh.id, 'alex', fresh.digest)
        db.commit()


async def test_forged_delegated_decision_cannot_reach_existing_executor(api, project, monkeypatch):
    sid = await world(api, project)
    service = api.app.state.supervisor.tools
    rid = service.request_session(sid, manager='npm', name='prettier', reason='Format')
    carried = []
    async def carry(row):
        carried.append(row.id)
        return {'ok': True, 'version': '3.6.2'}
    monkeypatch.setattr(service, '_carry', carry)
    with write_db(api.app.state.db) as db:
        row = db.get(ToolRequest, rid)
        row.status, row.decision = 'approved', {'source': 'C'}
        db.commit()
    await service.execute(rid)
    assert carried == []
    with api.app.state.db() as db:
        assert db.get(ToolRequest, rid).status == 'failed'


async def test_private_workspace_main_consent_requires_promotion(api, project):
    from acc.server import approvals
    sid = await world(api, project)
    with write_db(api.app.state.db) as db:
        p = db.get(Session, sid).project
        p.private_workspace, p.remote_url = True, 'https://git.example.lan/demo/private.git'
        with pytest.raises(approvals.Refused, match='promotion'):
            approvals.grant(db, actor='alex', project_id=p.id, team_id=sid, action='main.push',
                constraints={'repository': p.workspace_path, 'remote': p.remote_url, 'ref': 'refs/heads/main',
                             'verification_policy': 'required-gate-v1'}, duration='once', request_digest='b' * 64)


async def test_person_approval_cannot_execute_a_changed_payload(api, project, monkeypatch):
    sid = await world(api, project)
    service = api.app.state.supervisor.tools
    rid = service.request_session(sid, manager='npm', name='prettier', reason='Format')
    calls = []
    async def carry(row):
        calls.append(row.name)
        return {'ok': True, 'version': '3.6.2'}
    monkeypatch.setattr(service, '_carry', carry)
    # Separate decision from scheduling to exercise the admission/execution race.
    monkeypatch.setattr(service, 'schedule', lambda *_: None)
    service.decide(rid, 'alex', True)
    with write_db(api.app.state.db) as db:
        db.get(ToolRequest, rid).name = 'typescript'
        db.commit()
    await service.execute(rid)
    assert calls == []
    with api.app.state.db() as db:
        assert db.get(ToolRequest, rid).status == 'failed'


async def test_worker_identity_cannot_select_itself_as_deciding_lead(api, project):
    from acc.server import approvals
    sid = await world(api, project)
    with write_db(api.app.state.db) as db:
        lead = db.get(Session, sid)
        db.add(Session(id='worker', project_id=lead.project_id, provider=lead.provider, name='Worker',
            tmux_name='worker', run_as=lead.run_as, owner=lead.owner, launch_spec_id=lead.launch_spec_id,
            team_id=sid, parent_session_id=sid, team_depth=1))
        issue(db, sid)
        db.flush()
        row = ToolRequest(id='request', session_id='worker', project_id=lead.project_id, owner='alex',
            account='alex-agent', host='local', manager='npm', name='prettier', version='3.6.2', operation='install')
        assert approvals.evaluate_tool(db, row, sid)['reason'] == 'child_request_requires_root_person'
        assert approvals.evaluate_tool(db, row, 'worker')['source'] == 'P'
        from acc.server.models import LaunchSpec
        db.get(Session, 'worker').launch_spec = LaunchSpec(provider='claude', inputs={
            **lead.launch_spec.inputs, 'permission_mode': 'plan'}, files={}, digest='review-only')
        db.flush()
        assert approvals.evaluate_tool(db, row, sid)['source'] == 'P'
        db.commit()


async def test_existing_team_start_records_lead_grant_provenance(api, project, env):
    from test_team import lead, request
    caller = await lead(api, project, env)
    started = await request(api, env, caller, 'start', name='Demo helper', agent='claude', profile=None, task='Build demo')
    assert started['ok'], started
    with api.app.state.db() as db:
        row = db.scalar(select(AuditLog).where(AuditLog.action == 'team.start', AuditLog.target == caller['id']))
        assert row.detail['decision']['source'] == 'L'
        assert row.detail['decision']['lead_id'] == caller['id']


async def test_two_workers_share_one_pending_operation_without_sharing_teams(api, project):
    sid = await world(api, project)
    with write_db(api.app.state.db) as db:
        lead = db.get(Session, sid)
        import json

        from acc.server.child_caps import FILE, proof
        from acc.server.models import LaunchSpec
        for worker_id in ('worker-a', 'worker-b'):
            db.add(Session(id=worker_id, project_id=lead.project_id, provider=lead.provider, name=worker_id,
                tmux_name=worker_id, run_as=lead.run_as, owner=lead.owner, launch_spec=LaunchSpec(provider=lead.provider, inputs=lead.launch_spec.inputs,
                    files={**lead.launch_spec.files, FILE: json.dumps(proof(db, lead))}, digest=worker_id),
                team_id=sid, parent_session_id=sid, team_depth=1))
        db.commit()
    service = api.app.state.supervisor.tools
    a = service.request_session('worker-a', manager='npm', name='prettier', reason='Format first module')
    b = service.request_session('worker-b', manager='npm', name='prettier', reason='Format second module')
    assert a == b
    with api.app.state.db() as db:
        assert len(list(db.scalars(select(ToolRequest)))) == 1


async def test_existing_request_path_audits_shadow_match_without_approving(api, project):
    sid = await world(api, project)
    with write_db(api.app.state.db) as db:
        consent = issue(db, sid)
        db.commit()
    rid = api.app.state.supervisor.tools.request_session(sid, manager='npm', name='prettier', version='3.6.2', reason='Format')
    with api.app.state.db() as db:
        row = db.get(ToolRequest, rid)
        assert row.status == 'pending_approval' and row.decided_by is None
        shadow = db.scalar(select(AuditLog).where(AuditLog.action == 'approval.shadow', AuditLog.target == rid))
        assert shadow.detail['source'] == 'C' and shadow.detail['consent_id'] == consent.id
        assert shadow.detail['executed'] is False


async def test_project_deletion_can_detach_history_without_reusing_consent(api, project):
    from acc.server import approvals
    from acc.server.models import ApprovalConsent
    sid = await world(api, project)
    with write_db(api.app.state.db) as db:
        row = issue(db, sid)
        cid = row.id
        # Project deletion detaches nullable project links while keeping history.
        row.project_id = None
        db.commit()
    with api.app.state.db() as db:
        row = db.get(ApprovalConsent, cid)
        assert row.authority['project_id'] == project['id']
        with pytest.raises(approvals.Refused):
            approvals.current(db, row)


async def test_concurrent_revoke_and_claim_cannot_leave_usable_lease(api, project):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from acc.server import approvals
    from acc.server.models import ApprovalOperation
    sid = await world(api, project)
    service = api.app.state.supervisor.tools
    rid = service.request_session(sid, manager='npm', name='prettier', version='3.6.2', reason='Format')
    with write_db(api.app.state.db) as db:
        consent = issue(db, sid)
        cid = consent.id
        db.commit()
    barrier = Barrier(2)
    def claim():
        barrier.wait(timeout=10)
        with write_db(api.app.state.db) as db:
            try:
                op = approvals.claim_tool(db, db.get(ToolRequest, rid), sid)
                db.commit()
                return op.id
            except approvals.Refused:
                return None
    def revoke():
        barrier.wait(timeout=10)
        with write_db(api.app.state.db) as db:
            approvals.revoke(db, cid, 'alex', 1)
            db.commit()
    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed, revoked = pool.submit(claim), pool.submit(revoke)
        op_id = claimed.result(timeout=20)
        revoked.result(timeout=20)
    with write_db(api.app.state.db) as db:
        if op_id:
            assert db.get(ApprovalOperation, op_id).state == 'cancelled'
            with pytest.raises(approvals.Refused):
                approvals.consume(db, op_id)
        assert db.get(ToolRequest, rid).status == 'pending_approval'


async def test_cached_orm_consent_cannot_hide_a_committed_revocation(api, project):
    from sqlalchemy import update

    from acc.server import approvals
    from acc.server.models import ApprovalConsent
    sid = await world(api, project)
    with write_db(api.app.state.db) as db:
        row = issue(db, sid)
        # Database-side invalidation without ORM cache synchronization models
        # a stale cached object at a READ COMMITTED authorization boundary.
        db.execute(update(ApprovalConsent).where(ApprovalConsent.id == row.id).values(
            state='revoked', revision=2).execution_options(synchronize_session=False))
        with pytest.raises(approvals.Refused, match='revoked'):
            approvals.current(db, row)
