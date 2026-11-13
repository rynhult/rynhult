"""Service-owned install journal, without real catalog publication or execution."""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from helpers import write_db
from sqlalchemy import select
from test_delegated_approvals import world
from test_install_artifact import PACKAGE, archive

from acc import install_artifact as artifact
from acc.broker.account_install import ADAPTER, publication_plan, qualification_fingerprint
from acc.server import account_installs as installs
from acc.server import approvals, install_catalog
from acc.server.models import (
    AccountInstallJournal,
    InstallRecipe,
    Session,
    Setting,
    Team,
    utcnow,
)


@pytest.fixture(autouse=True)
def local_account_records(monkeypatch):
    def lookup(account):
        if account != 'alex-agent':
            raise KeyError(account)
        return SimpleNamespace(pw_uid=1001)
    monkeypatch.setattr(installs, 'pwd', SimpleNamespace(getpwnam=lookup))


def catalog(db):
    inspected, _ = artifact.inspect(archive(), PACKAGE)
    recipe = {'id': 'sample', 'revision': 1, 'adapter': ADAPTER, 'name': PACKAGE['name'],
        'version': PACKAGE['version'], 'registry': artifact.REGISTRY,
        'artifact_sha512': inspected['artifact_sha512'], 'metadata_digest': artifact.digest(PACKAGE),
        'manifest': inspected['manifest'], 'manifest_digest': inspected['manifest_digest'],
        'bins': inspected['bins'], 'runtime_id': 'a'*32, 'limits': dict(artifact.LIMITS)}
    identity = artifact.digest(recipe)
    db.add(InstallRecipe(digest=identity, recipe_id='sample', recipe=recipe, metadata_entry=PACKAGE,
        artifact=archive(), packument_digest='b'*64, reviewer='reviewer', approver='alex'))
    db.flush()
    return recipe, identity


def enable(monkeypatch, identity):
    fingerprint = qualification_fingerprint(identity, 'a'*32)
    monkeypatch.setattr(approvals, 'QUALIFIED_TOOL_EXECUTORS', frozenset({fingerprint}))


def grant(db, sid, identity, **kwargs):
    return approvals.grant(db, actor='alex', project_id=db.get(Session, sid).project_id,
        team_id=sid, action='tool.install', duration='team', constraints={
        'recipe_id': 'sample', 'recipe_digest': identity, 'uid': 1001, 'runtime_id': 'a'*32, 'max_installs': 5}, **kwargs)


def staged_report(operation_id, recipe):
    return {'operation_id': operation_id, 'manifest': recipe['manifest'],
        'manifest_digest': recipe['manifest_digest'], 'home_identity': [1, 2, 1001],
        'plan': publication_plan({'package': PACKAGE, 'manifest': recipe['manifest'], 'bins': recipe['bins']})}


async def test_qualification_digest_only_and_registry_bump(api, project, monkeypatch):
    sid = await world(api, project)
    with write_db(api.app.state.db) as db:
        _, identity = catalog(db)
        for values in (frozenset(), frozenset({'npm'})):
            monkeypatch.setattr(approvals, 'QUALIFIED_TOOL_EXECUTORS', values)
            with pytest.raises(approvals.Refused, match='runtime_unqualified'):
                grant(db, sid, identity)
        enable(monkeypatch, identity)
        consent = grant(db, sid, identity)
        db.flush()
        monkeypatch.setattr(approvals, 'REGISTRY_VERSION', approvals.REGISTRY_VERSION + 1)
        with pytest.raises(approvals.Refused, match='authority changed'):
            approvals.current(db, consent)


async def test_two_phase_journal_and_identical_replay(api, project, monkeypatch):
    sid = await world(api, project)
    with write_db(api.app.state.db) as db:
        recipe, identity = catalog(db)
        enable(monkeypatch, identity)
        grant(db, sid, identity)
        payload = {'recipe_id': 'sample', 'recipe_digest': identity}
        row, fresh = installs.admit(db, sid, 'c'*32, payload)
        assert fresh and row.state == 'queued'
        envelope, blob = installs.dispatch(db, row.id)
        assert row.state == 'dispatched' and blob == archive()
        with pytest.raises(approvals.Refused):
            installs.dispatch(db, row.id)
        report = staged_report(row.id, recipe)
        installs.staged(db, row.id, report)
        assert row.state == 'staged'
        installs.start_publish(db, row.id)
        assert row.state == 'publishing'
        for index, entry in enumerate(report['plan']):
            assert installs.intent(db, row.id, index) == entry
            installs.observed(db, row.id, index, {'path': entry['path'], 'created': True, 'parent_identity': [1, 3, 1001], 'identity': [1, index + 10, 1001]})
        footprint = [{k: v for k, v in e.items() if k not in ('source', 'shared')} | {'uid': 1001}
                     for e in report['plan'] if not e.get('shared')]
        installs.finish(db, row.id, {'state': 'installed', 'footprint': footprint,
                                     'footprint_digest': artifact.digest(footprint)})
        assert row.state == 'completed'
        db.merge(Setting(key='approvals.enabled', value=False))
        db.flush()
        old, fresh = installs.admit(db, sid, 'c'*32, payload)
        assert not fresh and old.id == row.id
        with pytest.raises(approvals.Refused):
            installs.admit(db, sid, 'c'*32, payload | {'recipe_digest': 'f'*64})


@pytest.mark.parametrize('change', ['revoke', 'disable', 'generation', 'recipe', 'lease', 'account'])
async def test_changes_between_stage_and_publish_refuse(api, project, monkeypatch, change):
    sid = await world(api, project)
    with write_db(api.app.state.db) as db:
        recipe, identity = catalog(db)
        enable(monkeypatch, identity)
        consent = grant(db, sid, identity)
        row, _ = installs.admit(db, sid, 'c'*32, {'recipe_id': 'sample', 'recipe_digest': identity})
        installs.dispatch(db, row.id)
        installs.staged(db, row.id, staged_report(row.id, recipe))
        db.flush()
        if change == 'revoke': approvals.revoke(db, consent.id, 'alex', consent.revision)
        if change == 'disable': db.merge(Setting(key='approvals.enabled', value=False))
        if change == 'generation': db.get(Team, sid).lifetime_revision += 1
        if change == 'recipe': db.get(InstallRecipe, identity).withdrawn = True
        if change == 'lease': db.get(AccountInstallJournal, row.id).lease_deadline = utcnow() - timedelta(seconds=1)
        if change == 'account': db.get(Session, sid).run_as = 'other'
        db.flush()
        with pytest.raises((approvals.Refused, artifact.InvalidArtifact)):
            installs.start_publish(db, row.id)
        assert row.state == 'staged'


async def test_failed_dispatches_count_quota_and_uncertain_blocks_account(api, project, monkeypatch):
    sid = await world(api, project)
    with write_db(api.app.state.db) as db:
        _, identity = catalog(db)
        enable(monkeypatch, identity)
        grant(db, sid, identity)
        for index in range(5):
            row, _ = installs.admit(db, sid, str(index)*32, {'recipe_id': 'sample', 'recipe_digest': identity})
            installs.dispatch(db, row.id)
            installs.fail(db, row.id, 'executor_failed')
            db.flush()
        from acc.server.models import DelegatedAction
        for old in db.scalars(select(DelegatedAction)):
            old.created_at = utcnow() - timedelta(hours=25)
        db.flush()
        last, _ = installs.admit(db, sid, 'f'*32, {'recipe_id': 'sample', 'recipe_digest': identity})
        with pytest.raises(approvals.Refused, match='resource_limit'):
            installs.dispatch(db, last.id)
        assert len(list(db.scalars(select(AccountInstallJournal)))) == 6


async def test_lost_reply_recovery_never_retries_and_blocks(api, project, monkeypatch):
    sid = await world(api, project)
    with write_db(api.app.state.db) as db:
        _, identity = catalog(db)
        enable(monkeypatch, identity)
        grant(db, sid, identity)
        row, _ = installs.admit(db, sid, 'c'*32, {'recipe_id': 'sample', 'recipe_digest': identity})
        installs.dispatch(db, row.id)
        db.get(AccountInstallJournal, row.id).lease_deadline = utcnow() - timedelta(seconds=1)
        db.flush()
        assert installs.recover(db) == 1
        assert installs.recover(db) == 0
        assert row.state == 'uncertain'
        with pytest.raises(approvals.Refused, match='collision'):
            installs.admit(db, sid, 'd'*32, {'recipe_id': 'sample', 'recipe_digest': identity})
        assert installs.admit(db, sid, 'c'*32, {'recipe_id': 'sample', 'recipe_digest': identity}) == (row, False)


def test_isolated_inspection_matches():
    result = install_catalog.inspect_isolated(archive(), PACKAGE)
    assert result == artifact.inspect(archive(), PACKAGE)[0]


async def test_security_configuration_atomic_rollback_and_revision(api, project):
    from test_delegated_approvals_round2 import sign_in

    from acc.server.models import User
    sid = await world(api, project)
    with write_db(api.app.state.db) as db:
        db.scalar(select(User).where(User.username == 'alex')).role = 'owner'
        db.merge(Setting(key='approvals.enabled', value=False))
        db.commit()
    await sign_in(api, 'alex', verified=True)
    config = (await api.get('/api/approvals/configuration')).json()
    assert config['enabled'] is False and config['recipes'] == []
    consent = {'project_id': project['id'], 'team_id': sid, 'action': 'port.reserve', 'duration': 'team',
               'constraints': {'range': 'dev', 'max_reservations': 2}}
    body = {k: config[k] for k in ('revision', 'security_revision')}
    bad = await api.post('/api/approvals/configuration', json=body | {'enabled': True,
        'consents': [consent, consent | {'constraints': {'range': 'unknown', 'max_reservations': 1}}]})
    assert bad.status_code == 422, bad.text
    with api.app.state.db() as db:
        from acc.server.models import ApprovalConsent
        assert not approvals.enabled(db)
        assert not list(db.scalars(select(ApprovalConsent)))
    response = await api.post('/api/approvals/configuration', json=body | {'enabled': True, 'consents': [consent]})
    assert response.status_code == 200, response.text
    assert len(response.json()['consents']) == 1
    stale = await api.post('/api/approvals/configuration', json=body | {'enabled': False, 'consents': []})
    assert stale.status_code == 409
    with api.app.state.db() as db:
        assert approvals.enabled(db)


async def test_security_configuration_install_token_and_unverified_refused(api, project):
    assert (await api.get('/api/approvals/configuration')).status_code == 403
    assert (await api.post('/api/approvals/configuration', json={})).status_code == 403
    await world(api, project)
    from test_delegated_approvals_round2 import sign_in
    await sign_in(api, 'alex')
    assert (await api.post('/api/approvals/configuration', json={})).status_code == 403


async def test_noop_rehash_report_bound_and_hourly_quota(api, project, monkeypatch):
    sid = await world(api, project)
    with write_db(api.app.state.db) as db:
        recipe, identity = catalog(db)
        enable(monkeypatch, identity)
        grant(db, sid, identity)
        for index in range(11):
            row, _ = installs.admit(db, sid, f'{index:032x}', {'recipe_id': 'sample', 'recipe_digest': identity})
            report = staged_report(row.id, recipe)
            footprint = [{k: v for k, v in e.items() if k not in ('source', 'shared')} | {'uid': 1001}
                         for e in report['plan'] if not e.get('shared')]
            observed = {'state': 'already_installed', 'home_identity': [1, 2, 1001], 'footprint': footprint,
                        'footprint_digest': artifact.digest(footprint)}
            if index == 10:
                with pytest.raises(approvals.Refused, match='resource_limit'):
                    installs.footprint(db, row.id, observed)
            else:
                assert installs.footprint(db, row.id, observed)
                assert db.get(AccountInstallJournal, row.id).dispatched_at is None
                db.flush()


@pytest.mark.parametrize('revoke_mid_stage', [False, True])
async def test_orchestrator_drives_real_adapter_in_disposable_prefix(api, project, monkeypatch, tmp_path, revoke_mid_stage, install_test_uid_policy):
    import os
    from pathlib import Path

    from acc.broker import account_install as adapter
    from acc.broker.config import BrokerConfig
    from acc.server.orchestrator.delegation import install
    sid = await world(api, project)
    home, run = tmp_path / 'prefix', tmp_path / 'install-run'
    home.mkdir()
    run.mkdir()
    monkeypatch.setattr(Path, 'home', lambda: home)
    with write_db(api.app.state.db) as db:
        _, identity = catalog(db)
        enable(monkeypatch, identity)
        consent = grant(db, sid, identity)
        monkeypatch.setattr(installs.pwd, 'getpwnam', lambda account: SimpleNamespace(pw_uid=os.geteuid()))
        consent.constraints = consent.constraints | {'uid': os.geteuid()}
        consent_id = consent.id
        db.commit()
    monkeypatch.setattr(adapter, 'QUALIFIED_TOOL_EXECUTORS', approvals.QUALIFIED_TOOL_EXECUTORS)
    protocol = adapter.Protocol(BrokerConfig(run_dir=run), adapter.AccountLock(run), 'a'*32, home=home)
    phases = []
    async def call(method, **params):
        assert method == 'account_install'
        assert params.pop('account') == 'alex-agent'
        assert params.pop('host') == 'local'
        phases.append(params['phase'])
        result = protocol.call(params)
        if revoke_mid_stage and params['phase'] == 'stage':
            with write_db(api.app.state.db) as db:
                approvals.revoke(db, consent_id, 'alex', 1)
                db.commit()
        return result
    monkeypatch.setattr(api.app.state.supervisor.broker, 'call', call)
    payload = {'request_key': 'a'*32, 'recipe_id': 'sample', 'recipe_digest': identity}
    result = await install(api.app.state.supervisor, sid, payload)
    assert result['state'] == ('failed' if revoke_mid_stage else 'completed'), result
    if revoke_mid_stage:
        assert 'publish' not in phases
        assert not list(home.iterdir())
    else:
        assert (home / '.local/bin/sample').read_bytes().startswith(b'#!')
        assert phases.index('stage') < phases.index('publish') < phases.index('finish')
        replay = await install(api.app.state.supervisor, sid, payload)
        assert replay == result
        noop = await install(api.app.state.supervisor, sid, payload | {'request_key': 'b'*32})
        assert noop['result']['state'] == 'already_installed', noop
    assert not protocol.extractor.operations


@pytest.mark.parametrize('changed', [False, True])
async def test_person_reconciliation_requires_current_reviewed_footprint(api, project, monkeypatch, changed):
    from test_delegated_approvals_round2 import sign_in
    sid = await world(api, project)
    with write_db(api.app.state.db) as db:
        _, identity = catalog(db)
        enable(monkeypatch, identity)
        consent = grant(db, sid, identity)
        row, _ = installs.admit(db, sid, 'c'*32, {'recipe_id': 'sample', 'recipe_digest': identity})
        installs.dispatch(db, row.id)
        installs.fail(db, row.id, 'transport_lost')
        operation_id = row.id
        approvals.revoke(db, consent.id, 'alex', consent.revision)
        db.merge(Setting(key='approvals.enabled', value=False))
        db.commit()
    assert (await api.get(f'/api/approvals/installs/{operation_id}')).status_code == 403
    await sign_in(api, 'alex', verified=True)
    report = {'state':'absent', 'home_identity':[1, 2, 1001]}
    async def call(method, **params):
        assert method == 'account_install' and params['phase'] == 'footprint'
        assert params['account'] == 'alex-agent' and params['host'] == 'local'
        return report
    monkeypatch.setattr(api.app.state.supervisor.broker, 'call', call)
    preview = await api.get(f'/api/approvals/installs/{operation_id}?inspect=true')
    assert preview.status_code == 200, preview.text
    expected = preview.json()['inspection_digest']
    if changed:
        report['home_identity'] = [1, 3, 1001]
    response = await api.post(f'/api/approvals/installs/{operation_id}/reconcile', json={'expected_digest':expected})
    assert response.status_code == (409 if changed else 200), response.text
    with api.app.state.db() as db:
        from acc.server.models import DelegatedAction
        row = db.get(DelegatedAction, operation_id)
        assert row.state == ('uncertain' if changed else 'failed')
        assert db.get(AccountInstallJournal, operation_id).dispatched_at is not None


async def test_configuration_rechecks_owner_after_revision_lock(api, project, monkeypatch):
    from test_delegated_approvals_round2 import sign_in

    from acc.server.models import User
    await world(api, project)
    with write_db(api.app.state.db) as db:
        db.scalar(select(User).where(User.username == 'alex')).role = 'owner'
        db.commit()
    await sign_in(api, 'alex', verified=True)
    config = (await api.get('/api/approvals/configuration')).json()
    original = approvals.security.lock_revision
    def demote_after_lock(db):
        original(db)
        user = db.scalar(select(User).where(User.username == 'alex'))
        user.role = 'member'
        db.flush()
    monkeypatch.setattr(approvals.security, 'lock_revision', demote_after_lock)
    response = await api.post('/api/approvals/configuration', json={
        'enabled':False, 'consents':[], 'revision':config['revision'], 'security_revision':config['security_revision']})
    assert response.status_code == 403, response.text


async def test_missing_managed_paths_need_person_and_refusal_audit_is_bounded(api, project, monkeypatch):
    from acc.server.models import AuditLog
    sid = await world(api, project)
    with write_db(api.app.state.db) as db:
        recipe, identity = catalog(db)
        enable(monkeypatch, identity)
        grant(db, sid, identity)
        row, _ = installs.admit(db, sid, 'a'*32, {'recipe_id':'sample', 'recipe_digest':identity})
        report = staged_report(row.id, recipe)
        footprint = [{k:v for k,v in e.items() if k not in ('source','shared')} | {'uid':1001}
                     for e in report['plan'] if not e.get('shared')]
        assert installs.footprint(db, row.id, {'state':'already_installed','home_identity':[1,2,1001],
            'footprint':footprint,'footprint_digest':artifact.digest(footprint)})
        db.flush()
        for key in ('b','c'):
            attempt, _ = installs.admit(db, sid, key*32, {'recipe_id':'sample','recipe_digest':identity})
            with pytest.raises(approvals.Refused, match='collision'):
                installs.footprint(db, attempt.id, {'state':'absent','home_identity':[1,2,1001]})
            installs.fail(db, attempt.id, 'collision')
            db.flush()
        assert len(list(db.scalars(select(AuditLog).where(AuditLog.action=='approval.refused')))) == 1
        assert len(list(db.scalars(select(AuditLog).where(AuditLog.action=='approval.claim')))) == 1


async def test_requalified_runtime_cannot_borrow_an_old_stage(api, project, monkeypatch):
    sid = await world(api, project)
    with write_db(api.app.state.db) as db:
        recipe, identity = catalog(db)
        enable(monkeypatch, identity)
        grant(db, sid, identity)
        row, _ = installs.admit(db, sid, 'c'*32, {'recipe_id':'sample','recipe_digest':identity})
        installs.dispatch(db, row.id)
        installs.staged(db, row.id, staged_report(row.id, recipe))
        db.flush()
        monkeypatch.setattr(installs, 'qualification_fingerprint', lambda *_: 'f'*64)
        monkeypatch.setattr(approvals, 'QUALIFIED_TOOL_EXECUTORS', frozenset({'f'*64}))
        with pytest.raises(approvals.Refused, match='authority_changed'):
            installs.start_publish(db, row.id)
        assert row.state == 'staged'


async def test_consent_inspection_shows_remaining_family_cap_only_while_current(api, project):
    from test_delegated_approvals_round2 import sign_in

    from acc.server.delegated_execution import reserve_port
    sid = await world(api, project)
    with write_db(api.app.state.db) as db:
        consent = approvals.grant(db, actor='alex', project_id=project['id'], team_id=sid,
            action='port.reserve', duration='team', constraints={'range':'dev','max_reservations':2})
        consent_id = consent.id
        reserve_port(db, sid, 'a'*32, 'dev')
        db.commit()
    await sign_in(api, 'alex')
    rows = (await api.get('/api/approvals/consents')).json()['consents']
    assert next(r for r in rows if r['id']==consent_id)['remaining_allowance'] == 1
    with write_db(api.app.state.db) as db:
        db.merge(Setting(key='approvals.enabled', value=False))
        db.commit()
    rows = (await api.get('/api/approvals/consents')).json()['consents']
    blocked = next(r for r in rows if r['id']==consent_id)
    assert blocked['effective_state'] == 'blocked' and blocked['remaining_allowance'] is None


@pytest.mark.parametrize('version', ['1.2.2', '1.2.3'])
async def test_legacy_person_pin_prevents_updates_and_missing_path_repair(api, project, monkeypatch, version):
    from acc.server.models import InstalledTool
    sid = await world(api, project)
    rid = api.app.state.supervisor.tools.request_session(sid, manager='npm', name=PACKAGE['name'], version=version, reason='Person pin fixture')
    with write_db(api.app.state.db) as db:
        _, identity = catalog(db)
        enable(monkeypatch, identity)
        grant(db, sid, identity)
        db.add(InstalledTool(id='e'*64, owner='alex', host='local', account='alex-agent', manager='npm',
            name=PACKAGE['name'], version=version, request_id=rid))
        db.flush()
        if version != PACKAGE['version']:
            with pytest.raises(approvals.Refused, match='collision'):
                installs.admit(db, sid, 'a'*32, {'recipe_id':'sample','recipe_digest':identity})
        else:
            row, _ = installs.admit(db, sid, 'a'*32, {'recipe_id':'sample','recipe_digest':identity})
            journal = db.get(AccountInstallJournal, row.id)
            assert journal.envelope['provenance']['managed_versions']['person_pin']['version'] == version
            with pytest.raises(approvals.Refused, match='collision'):
                installs.footprint(db, row.id, {'state':'absent','home_identity':[1,2,1001]})


async def test_person_pin_change_between_stage_and_publish_is_fenced(api, project, monkeypatch):
    from acc.server.models import InstalledTool
    sid = await world(api, project)
    rid = api.app.state.supervisor.tools.request_session(sid, manager='npm', name=PACKAGE['name'], version='1.2.3', reason='Person pin fixture')
    with write_db(api.app.state.db) as db:
        recipe, identity = catalog(db)
        enable(monkeypatch, identity)
        grant(db, sid, identity)
        pin = InstalledTool(id='e'*64, owner='alex', host='local', account='alex-agent', manager='npm',
            name=PACKAGE['name'], version='1.2.3', request_id=rid)
        db.add(pin)
        db.flush()
        row, _ = installs.admit(db, sid, 'a'*32, {'recipe_id':'sample','recipe_digest':identity})
        installs.dispatch(db, row.id)
        installs.staged(db, row.id, staged_report(row.id, recipe))
        pin.version = '1.2.4'
        db.flush()
        with pytest.raises(approvals.Refused, match='footprint_mismatch'):
            installs.start_publish(db, row.id)


async def test_explicit_person_removal_retires_previous_managed_observation(api, project, monkeypatch):
    from acc.server.models import ToolRequest
    sid = await world(api, project)
    with write_db(api.app.state.db) as db:
        recipe, identity = catalog(db)
        enable(monkeypatch, identity)
        grant(db, sid, identity)
        row, _ = installs.admit(db, sid, 'a'*32, {'recipe_id':'sample','recipe_digest':identity})
        report = staged_report(row.id, recipe)
        footprint = [{k:v for k,v in e.items() if k not in ('source','shared')} | {'uid':1001}
                     for e in report['plan'] if not e.get('shared')]
        installs.footprint(db, row.id, {'state':'already_installed','home_identity':[1,2,1001],
            'footprint':footprint,'footprint_digest':artifact.digest(footprint)})
        db.commit()
    rid = api.app.state.supervisor.tools.request_session(sid, manager='npm', name=PACKAGE['name'],
        action='remove', reason='Explicit person removal fixture')
    with write_db(api.app.state.db) as db:
        removal = db.get(ToolRequest, rid)
        removal.status, removal.completed_at, removal.decision = 'applied', utcnow(), {'source':'P'}
        db.flush()
        row, _ = installs.admit(db, sid, 'b'*32, {'recipe_id':'sample','recipe_digest':identity})
        assert db.get(AccountInstallJournal, row.id).envelope['provenance']['managed_versions']['version'] is None
        assert not installs.footprint(db, row.id, {'state':'absent','home_identity':[1,2,1001]})


async def test_accd_refuses_consent_uid_different_from_passwd(api, project, monkeypatch):
    sid = await world(api, project)
    with write_db(api.app.state.db) as db:
        _, identity = catalog(db)
        enable(monkeypatch, identity)
        monkeypatch.setattr(installs.pwd, 'getpwnam', lambda account: SimpleNamespace(pw_uid=9999))
        with pytest.raises(approvals.Refused, match='authority_changed'):
            grant(db, sid, identity)
        monkeypatch.setattr(installs.pwd, 'getpwnam', lambda account: SimpleNamespace(pw_uid=1001))
        grant(db, sid, identity)
        row, _ = installs.admit(db, sid, 'c' * 32, {'recipe_id': 'sample', 'recipe_digest': identity})
        installs.dispatch(db, row.id)
        monkeypatch.setattr(installs.pwd, 'getpwnam', lambda account: SimpleNamespace(pw_uid=9999))
        with pytest.raises(approvals.Refused, match='authority_changed'):
            installs.current(db, row.id)


@pytest.mark.parametrize('uid', [0, None, True])
def test_accd_refuses_unknown_or_root_account_uid(monkeypatch, uid):
    monkeypatch.setattr(installs.pwd, 'getpwnam', lambda account: SimpleNamespace(pw_uid=uid))
    with pytest.raises(approvals.Refused, match='authority_changed'):
        installs.account_uid('alex-agent')


def test_accd_refuses_missing_passwd_record():
    with pytest.raises(approvals.Refused, match='authority_changed'):
        installs.account_uid('missing')


@pytest.mark.parametrize('broker_uid', [1001, 9999])
async def test_configuration_checks_broker_uid_against_accd_passwd(api, project, monkeypatch, broker_uid):
    from test_delegated_approvals_round2 import sign_in
    sid = await world(api, project)
    with write_db(api.app.state.db) as db:
        _, identity = catalog(db)
        enable(monkeypatch, identity)
        db.commit()
    await sign_in(api, 'alex', verified=True)
    async def call(method, **params):
        if method == 'account_install':
            return {'uid': broker_uid, 'runtime_id': 'a' * 32, 'untrusted_extra': 'ignored'}
        return {}
    monkeypatch.setattr(api.app.state.supervisor.broker, 'call', call)
    response = await api.get('/api/approvals/configuration', params={'team_id': sid})
    assert response.status_code == 200, response.text
    data = response.json()
    assert len(data['recipes']) == (1 if broker_uid == 1001 else 0)
    assert data.get('install_identity') == ({'uid': 1001, 'runtime_id': 'a' * 32} if broker_uid == 1001 else None)


async def test_consents_endpoints_invalidate_configuration_drafts(api, project):
    from test_delegated_approvals_round2 import sign_in
    sid = await world(api, project)
    await sign_in(api, 'alex', verified=True)
    draft = (await api.get('/api/approvals/configuration')).json()
    consent = {'project_id': project['id'], 'team_id': sid, 'action': 'port.reserve', 'duration': 'project',
               'constraints': {'range': 'dev', 'max_reservations': 2}}
    response = await api.post('/api/approvals/consents', json=consent)
    assert response.status_code == 201, response.text
    parent = response.json()
    assert (await api.get('/api/approvals/configuration')).json()['revision'] == draft['revision'] + 1
    response = await api.post(f"/api/approvals/consents/{parent['id']}/bind", json={'team_id': sid})
    assert response.status_code == 201, response.text
    assert (await api.get('/api/approvals/configuration')).json()['revision'] == draft['revision'] + 2
    response = await api.post(f"/api/approvals/consents/{parent['id']}/revoke", json={'revision': parent['revision']})
    assert response.status_code == 200, response.text
    assert (await api.get('/api/approvals/configuration')).json()['revision'] == draft['revision'] + 3
    response = await api.post('/api/approvals/configuration', json={key: draft[key] for key in
        ('enabled', 'revision', 'security_revision')} | {'consents': []})
    assert response.status_code == 409, response.text
