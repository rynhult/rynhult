"""Thin-path admission is current, exact, idempotent and scoped to the bound lead."""
import pytest
from fastapi import HTTPException
from helpers import write_db
from sqlalchemy import select
from test_delegated_approvals import world

from acc.server import approvals
from acc.server import delegated_execution as execution
from acc.server.models import ApprovalConsent, DelegatedAction, PortReservation, Session, Setting


def port_consent(db, sid, count=2):
    lead = db.get(Session, sid)
    return approvals.grant(db, actor=lead.owner, project_id=lead.project_id, team_id=sid,
        action='port.reserve', constraints={'range': 'dev', 'max_reservations': count}, duration='team')


async def test_default_off_without_erasing_prior_explicit_choice(api):
    with write_db(api.app.state.db) as db:
        assert not approvals.enabled(db)
        db.merge(Setting(key='approvals.enabled', value=True))
        db.flush()
        assert approvals.enabled(db)


async def test_next_free_bound_result_duplicate_and_aggregate_cap(api, project):
    sid = await world(api, project)
    with write_db(api.app.state.db) as db:
        consent = port_consent(db, sid)
        db.add(PortReservation(port=8000, label='Outside service', owner='alex'))
        db.flush()
        first = execution.reserve_port(db, sid, 'a'*32, 'dev')
        assert first.result['port'] == 8001
        assert first.decision['lead_id'] == sid and first.decision['consent_id'] == consent.id
        assert execution.reserve_port(db, sid, 'a'*32, 'dev').id == first.id
        assert execution.reserve_port(db, sid, 'b'*32, 'dev').result['port'] == 8002
        with pytest.raises(approvals.Refused, match='exhausted'):
            execution.reserve_port(db, sid, 'c'*32, 'dev')
        with pytest.raises(approvals.Refused, match='changed payload'):
            execution.reserve_port(db, sid, 'a'*32, 'prod')
        db.commit()
    with api.app.state.db() as db:
        assert len(list(db.scalars(select(DelegatedAction)))) == 2
        assert db.get(PortReservation, 8000).project_id is None


@pytest.mark.parametrize('change', ['no-consent', 'revoked', 'disabled', 'account', 'node', 'unknown', 'worker'])
async def test_denied_actions_have_no_port_side_effect(api, project, change):
    sid = await world(api, project)
    with write_db(api.app.state.db) as db:
        consent = port_consent(db, sid) if change != 'no-consent' else None
        caller = db.get(Session, sid)
        if change == 'revoked': approvals.revoke(db, consent.id, 'alex', consent.revision)
        if change == 'disabled': db.merge(Setting(key='approvals.enabled', value=False))
        if change == 'account': caller.run_as = 'other-agent'
        if change == 'node': caller.placement.host = 'other-node'
        if change == 'worker':
            db.add(Session(id='worker', project_id=caller.project_id, provider=caller.provider, name='Worker',
                tmux_name='worker', run_as=caller.run_as, owner=caller.owner, launch_spec_id=caller.launch_spec_id,
                team_id=sid, parent_session_id=sid, team_depth=1))
        db.flush()
        with pytest.raises(approvals.Refused):
            execution.admit(db, 'worker' if change == 'worker' else sid, 'a'*32,
                'unknown' if change == 'unknown' else 'port.reserve', {'range':'dev'})
        assert not list(db.scalars(select(PortReservation)))
        assert not list(db.scalars(select(DelegatedAction)))


async def test_revoke_and_disable_preserve_completed_outcome(api, project):
    sid = await world(api, project)
    with write_db(api.app.state.db) as db:
        consent = port_consent(db, sid)
        operation = execution.reserve_port(db, sid, 'a'*32, 'dev')
        db.merge(Setting(key='approvals.enabled', value=False))
        approvals.revoke(db, consent.id, 'alex', consent.revision)
        db.commit()
    with write_db(api.app.state.db) as db:
        row = execution.reserve_port(db, sid, 'a'*32, 'dev')
        assert row.id == operation.id and row.state == 'completed'
        assert db.get(ApprovalConsent, consent.id).state == 'revoked'
        with pytest.raises(approvals.Refused):
            execution.reserve_port(db, sid, 'b'*32, 'dev')


async def test_image_probe_rechecks_revocation_before_broker_side_effect(api, project, monkeypatch):
    from acc.server.orchestrator.delegation import image
    sid = await world(api, project)
    with write_db(api.app.state.db) as db:
        caller = db.get(Session, sid)
        caller.launch_spec.inputs = {**caller.launch_spec.inputs, 'model': 'fable'}
        db.flush()
        consent = approvals.grant(db, actor='alex', project_id=project['id'], team_id=sid,
            action='image.check', constraints={'provider':'claude','model':'fable','uid':1001,'runtime_id':'f'*32}, duration='team')
        consent_id = consent.id
        db.commit()
    calls = []
    broker = api.app.state.supervisor.broker
    original = broker.call
    async def call(method, **params):
        if method not in ('model_catalog', 'image_probe'):
            return await original(method, **params)
        calls.append(method)
        if method == 'model_catalog':
            with write_db(api.app.state.db) as db:
                approvals.revoke(db, consent_id, 'alex', 1)
                db.commit()
            return {'claude':[{'id':'fable','adapter_version':'a'*64,'cli_version':'1.2.3','runtime_id':'f'*32,'uid':1001,'delegation_protocol':'image-check-delegation-v1'}]}
        pytest.fail('revoked operation reached probe: ' + method)
    monkeypatch.setattr(api.app.state.supervisor.broker, 'call', call)
    result = await image(api.app.state.supervisor, sid, {'request_key':'a'*32,'provider':'claude','model':'fable'})
    assert result['state'] == 'failed'
    assert result['result']['image_input'] is None
    assert calls == ['model_catalog']
    duplicate = await image(api.app.state.supervisor, sid, {'request_key':'a'*32,'provider':'claude','model':'fable'})
    assert duplicate == result
    assert calls == ['model_catalog']


async def test_image_quota_survives_new_consent_and_unknown_provider_refuses(api, project):
    sid = await world(api, project)
    with write_db(api.app.state.db) as db:
        caller = db.get(Session, sid)
        caller.launch_spec.inputs = {**caller.launch_spec.inputs, 'model': 'fable'}
        db.flush()
        for index in range(3):
            approvals.grant(db, actor='alex', project_id=project['id'], team_id=sid,
                action='image.check', constraints={'provider':'claude','model':'fable','uid':1001,'runtime_id':'f'*32}, duration='team')
            if index < 2:
                execution.admit(db, sid, str(index)*32, 'image.check', {'provider':'claude','model':'fable'})
            else:
                with pytest.raises(approvals.Refused, match='exhausted'):
                    execution.admit(db, sid, str(index)*32, 'image.check', {'provider':'claude','model':'fable'})
        with pytest.raises(approvals.Refused):
            execution.admit(db, sid, 'f'*32, 'image.check', {'provider':'unknown','model':'fable'})


async def test_concurrent_duplicate_port_requests_return_one_reservation(api, project):
    import asyncio

    from acc.server.orchestrator.delegation import reserve
    sid = await world(api, project)
    with write_db(api.app.state.db) as db:
        port_consent(db, sid, count=1)
        db.commit()
    payload = {'request_key':'a'*32,'range':'dev'}
    first, second = await asyncio.gather(reserve(api.app.state.supervisor, sid, payload),
                                         reserve(api.app.state.supervisor, sid, payload))
    assert first == second
    with api.app.state.db() as db:
        assert len(list(db.scalars(select(PortReservation)))) == 1
        assert len(list(db.scalars(select(DelegatedAction)))) == 1


async def test_digest_retains_revoked_result_and_refuses_api_and_unrelated_project(api, project):
    from test_delegated_approvals_round2 import sign_in
    sid = await world(api, project)
    with write_db(api.app.state.db) as db:
        consent = port_consent(db, sid)
        operation = execution.reserve_port(db, sid, 'a'*32, 'dev')
        approvals.revoke(db, consent.id, 'alex', 1)
        db.merge(Setting(key='approvals.enabled', value=False))
        db.commit()
    assert (await api.get('/api/approvals/activity')).status_code == 403
    await sign_in(api, 'alex')
    response = await api.get('/api/approvals/activity')
    assert response.status_code == 200
    assert response.json()['operations'][0]['id'] == operation.id
    assert response.json()['operations'][0]['state'] == 'completed'
    assert (await api.get('/api/approvals/activity',params={'project_id':99999})).status_code == 404
    assert (await api.get('/api/approvals/activity',params={'limit':101})).status_code == 422


def test_execution_migration_fences_downgrade_even_for_failed_history(tmp_path):
    from datetime import timedelta

    from alembic import command
    from sqlalchemy import text
    from test_team_lifetime_migration import seed

    from acc.server.db import alembic_config, make_engine
    from acc.server.models import utcnow
    url = f'sqlite:///{tmp_path / "execution.db"}'
    config = alembic_config(url)
    command.upgrade(config, '0088')
    engine = make_engine(url)
    seed(engine, 'project', {'id':1,'slug':'demo','name':'Demo','workspace_path':'/tmp/example'})
    seed(engine, 'launch_spec', {'id':1,'provider':'claude','inputs':{},'files':{},'digest':'fixture'})
    seed(engine, 'session', {'id':'lead','project_id':1,'launch_spec_id':1,'provider':'claude','name':'Lead',
                            'tmux_name':'lead','run_as':'agent','team_id':'lead'})
    seed(engine, 'team', {'id':'lead','limits':{},'expires_at':utcnow()+timedelta(hours=1)})
    seed(engine, 'approval_consent', {'id':'consent','team_id':'lead','issuer':'alex','project_id':1})
    command.upgrade(config, '0089')
    seed(engine, 'delegated_action', {'id':'operation','lead_id':'lead','request_key':'a'*32,
                                     'consent_id':'consent','state':'failed'})
    try:
        with pytest.raises(RuntimeError, match='Delegated execution records exist'):
            command.downgrade(config, '0088')
        with engine.connect() as db:
            assert db.scalar(text('SELECT state FROM delegated_action')) == 'failed'
    finally:
        engine.dispose()


@pytest.mark.parametrize('verified', [True, None])
async def test_delegated_image_once_runs_existing_probe_and_preserves_honest_result(api, project, monkeypatch, verified):
    from acc.server.orchestrator.delegation import image
    sid = await world(api, project)
    payload = {'provider':'claude','model':'fable'}
    with write_db(api.app.state.db) as db:
        caller = db.get(Session, sid)
        caller.launch_spec.inputs = {**caller.launch_spec.inputs, 'model':'fable'}
        db.flush()
        consent = approvals.grant(db, actor='alex', project_id=project['id'], team_id=sid,
            action='image.check', constraints={**payload,'uid':1001,'runtime_id':'f'*32}, duration='once',
            request_digest=approvals.digest({'action':'image.check','payload':payload}))
        cid = consent.id
        db.commit()
    broker = api.app.state.supervisor.broker
    original = broker.call
    calls = []
    fingerprint = {'adapter_version':'a'*64,'cli_version':'1.2.3','runtime_id':'f'*32,'uid':1001,'delegation_protocol':'image-check-delegation-v1'}
    async def call(method, **params):
        if method not in ('model_catalog', 'image_probe'):
            return await original(method, **params)
        calls.append(method)
        assert params['account'] == 'alex-agent' and params['host'] == 'local'
        if method == 'model_catalog':
            return {'claude':[{'id':'fable',**fingerprint}]}
        assert params['expected_uid'] == 1001 and params['expected_runtime_id'] == 'f'*32
        proof = {'code':'AAYM','shapes':['red circle','green square','blue triangle']}
        return {**fingerprint,'image_input':verified,'evidence':{'outcome':'pass' if verified else 'error',
                'expected':proof,'observed':proof}}
    monkeypatch.setattr(broker, 'call', call)
    result = await image(api.app.state.supervisor, sid, {'request_key':'a'*32,**payload})
    assert result['state'] == ('completed' if verified else 'uncertain')
    assert result['result']['image_input'] is verified
    assert await image(api.app.state.supervisor, sid, {'request_key':'a'*32,**payload}) == result
    assert calls == ['model_catalog','image_probe']
    with api.app.state.db() as db:
        assert db.get(ApprovalConsent, cid).remaining == 0
    with pytest.raises(approvals.Refused):
        await image(api.app.state.supervisor, sid, {'request_key':'b'*32,**payload})


@pytest.mark.parametrize('change', ['unknown-protocol', 'uid', 'runtime'])
async def test_unknown_or_changed_image_broker_identity_never_dispatches(api, project, monkeypatch, change):
    from acc.server.orchestrator.delegation import image
    sid = await world(api, project)
    with write_db(api.app.state.db) as db:
        caller = db.get(Session, sid)
        caller.launch_spec.inputs = {**caller.launch_spec.inputs, 'model':'fable'}
        db.flush()
        approvals.grant(db, actor='alex', project_id=project['id'], team_id=sid, action='image.check',
            constraints={'provider':'claude','model':'fable','uid':1001,'runtime_id':'f'*32}, duration='team')
        db.commit()
    broker = api.app.state.supervisor.broker
    original = broker.call
    calls = []
    async def call(method, **params):
        if method not in ('model_catalog', 'image_probe'):
            return await original(method, **params)
        calls.append(method)
        assert method != 'image_probe', 'changed/unknown broker reached a probe'
        model = {'id':'fable','adapter_version':'a'*64,'cli_version':'1.2.3','runtime_id':'f'*32,
                 'uid':1001,'delegation_protocol':'image-check-delegation-v1'}
        if change == 'unknown-protocol': model.pop('delegation_protocol')
        if change == 'uid': model['uid'] = 1002
        if change == 'runtime': model['runtime_id'] = 'e'*32
        return {'claude':[model]}
    monkeypatch.setattr(broker, 'call', call)
    result = await image(api.app.state.supervisor, sid, {'request_key':'a'*32,'provider':'claude','model':'fable'})
    assert result['state'] == 'failed' and result['result']['image_input'] is None
    assert calls == ['model_catalog']

    from acc.server.models import AuditLog
    with api.app.state.db() as db:
        refusal = db.scalar(select(AuditLog).where(AuditLog.action == 'approval.refused'))
        assert refusal.detail['reason'] == 'Image check unavailable'


@pytest.mark.parametrize('changed', ['uid', 'runtime'])
def test_broker_rechecks_account_binding_before_starting_probe(env, monkeypatch, changed):
    import os

    from acc.broker import image_probe, ops
    uid = max(1, os.geteuid())
    monkeypatch.setattr(image_probe, 'identity', lambda *args: {'runtime_id':'f'*32,'cli_version':'1.2.3'})
    monkeypatch.setattr(image_probe, 'bounded', lambda *args, **kwargs: pytest.fail('mismatched binding started process'))
    with pytest.raises(ops.OpError, match='account/runtime changed'):
        image_probe.run(env['broker'], 'claude', 'fable',
                        expected_uid=uid+1 if changed == 'uid' else uid,
                        expected_runtime_id='e'*32 if changed == 'runtime' else 'f'*32)


@pytest.mark.parametrize('reason', ['no-consent', 'disabled', 'quota', 'scope'])
async def test_refusal_survives_rollback_is_rate_limited_and_visible_to_person(api, project, reason):
    from test_delegated_approvals_round2 import sign_in

    from acc.server.models import AuditLog
    from acc.server.orchestrator.delegation import reserve
    sid = await world(api, project)
    with write_db(api.app.state.db) as db:
        if reason != 'no-consent': port_consent(db, sid, count=1)
        if reason == 'disabled': db.merge(Setting(key='approvals.enabled', value=False))
        if reason == 'scope': db.get(Session, sid).run_as = 'other-agent'
        db.commit()
    if reason == 'quota':
        await reserve(api.app.state.supervisor, sid, {'request_key':'a'*32,'range':'dev'})
    for _ in range(3):
        with pytest.raises(approvals.Refused):
            await reserve(api.app.state.supervisor, sid, {'request_key':'b'*32,'range':'dev'})
    with api.app.state.db() as db:
        rows = list(db.scalars(select(AuditLog).where(AuditLog.action == 'approval.refused')))
        assert len(rows) == 1
        assert rows[0].detail['reason'] == {'no-consent':'No current consent', 'disabled':'Delegation disabled',
                                           'quota':'Allowance exhausted', 'scope':'Scope or consent mismatch'}[reason]
        assert len(list(db.scalars(select(PortReservation)))) == (1 if reason == 'quota' else 0)
    await sign_in(api, 'alex')
    history = (await api.get('/api/approvals/activity')).json()['refusals']
    assert len(history) == 1 and history[0]['reason'] == rows[0].detail['reason']
    from acc.server.models import User
    with write_db(api.app.state.db) as db:
        if db.scalar(select(User).where(User.username == 'casey')) is None:
            db.add(User(username='casey', password_hash='unused', totp_secret='unused', role='member', account='casey-agent'))
        db.commit()
    await sign_in(api, 'casey')
    assert (await api.get('/api/approvals/activity')).json()['refusals'] == []


async def test_recovery_marks_only_expired_dispatched_images_uncertain_without_resetting_quota(api, project):
    from datetime import timedelta

    from acc.server.models import AuditLog, utcnow
    sid = await world(api, project)
    with write_db(api.app.state.db) as db:
        caller = db.get(Session, sid)
        caller.launch_spec.inputs = {**caller.launch_spec.inputs, 'model':'fable'}
        db.flush()
        approvals.grant(db, actor='alex', project_id=project['id'], team_id=sid, action='image.check',
            constraints={'provider':'claude','model':'fable','uid':1001,'runtime_id':'f'*32}, duration='team')
        old, _ = execution.admit(db, sid, 'a'*32, 'image.check', {'provider':'claude','model':'fable'})
        recent, _ = execution.admit(db, sid, 'b'*32, 'image.check', {'provider':'claude','model':'fable'})
        old.created_at = utcnow() - timedelta(seconds=61)
        old_id, recent_id = old.id, recent.id
        db.commit()
    await execution.recover(api.app.state.supervisor)
    await execution.recover(api.app.state.supervisor)
    from test_delegated_approvals_round2 import sign_in
    await sign_in(api, 'alex')
    history = (await api.get('/api/approvals/activity')).json()['operations']
    assert next(row for row in history if row['id'] == old_id)['state'] == 'uncertain'
    with write_db(api.app.state.db) as db:
        old = db.get(DelegatedAction, old_id)
        assert old.state == 'uncertain' and old.result == {'image_input':None,'reason':'Image check interrupted'}
        assert db.get(DelegatedAction, recent_id).state == 'dispatched'
        assert len(list(db.scalars(select(AuditLog).where(AuditLog.action == 'approval.result', AuditLog.target == old_id)))) == 1
        with pytest.raises(approvals.Refused, match='exhausted'):
            execution.admit(db, sid, 'c'*32, 'image.check', {'provider':'claude','model':'fable'})


async def test_delegated_port_uses_project_owner_and_normal_port_audit(api, project):
    from acc.server.models import AuditLog, User
    sid = await world(api, project)
    with write_db(api.app.state.db) as db:
        db.scalar(select(User).where(User.username == 'alex')).role = 'owner'
        db.get(Session, sid).project.owner = 'member-owner'
        port_consent(db, sid)
        result = execution.reserve_port(db, sid, 'a'*32, 'dev')
        db.flush()
        assert db.get(PortReservation, result.result['port']).owner == 'member-owner'
        audit = db.scalar(select(AuditLog).where(AuditLog.action == 'port.reserve'))
        assert audit.actor == 'session:' + sid
        assert audit.target == str(result.result['port'])
        assert audit.detail == {'project':'demo','delegated':result.id}


@pytest.mark.parametrize('error,reason', [(RuntimeError('/private/canary-secret'), 'Internal error'),
    (approvals.Refused('/private/canary-secret'), 'Scope or consent mismatch'),
    (HTTPException(409, '/private/canary-secret'), 'Image check unavailable')])
def test_failure_reasons_are_fixed_and_never_exception_prose(error, reason):
    assert execution.failure_reason(error) == reason


async def test_unexpected_image_failure_is_stored_and_shown_without_internal_text(api, project, monkeypatch):
    from test_delegated_approvals_round2 import sign_in

    from acc.server.api import operations
    from acc.server.orchestrator.delegation import image
    sid = await world(api, project)
    with write_db(api.app.state.db) as db:
        caller = db.get(Session, sid)
        caller.launch_spec.inputs = {**caller.launch_spec.inputs, 'model':'fable'}
        db.flush()
        approvals.grant(db, actor='alex', project_id=project['id'], team_id=sid, action='image.check',
            constraints={'provider':'claude','model':'fable','uid':1001,'runtime_id':'f'*32}, duration='team')
        db.commit()
    async def fail(*args, **kwargs):
        raise RuntimeError('/private/canary-secret')
    monkeypatch.setattr(operations, 'run_image_check', fail)
    result = await image(api.app.state.supervisor, sid, {'request_key':'a'*32,'provider':'claude','model':'fable'})
    assert result['state'] == 'failed' and result['result']['reason'] == 'Internal error'
    with api.app.state.db() as db:
        assert db.get(DelegatedAction, result['id']).result == {'image_input':None,'reason':'Internal error'}
    await sign_in(api, 'alex')
    history = await api.get('/api/approvals/activity')
    assert 'Internal error' in history.text and 'canary-secret' not in history.text


async def test_port_executor_filters_the_explicit_session_port_set(api, project):
    from acc.server.session_caps import narrow
    sid = await world(api, project)
    with write_db(api.app.state.db) as db:
        narrow(db, db.get(Session, sid), {'ports': [8003]}, 'alex')
        port_consent(db, sid)
        row = execution.reserve_port(db, sid, 'e' * 32, 'dev')
        assert row.result['port'] == 8003
        assert db.get(PortReservation, 8000) is None
        assert db.get(PortReservation, 8001) is None
        db.commit()
