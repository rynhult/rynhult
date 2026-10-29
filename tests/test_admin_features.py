"""Owner feature controls: defaults, explicit configuration and stale-tab safety."""
import pytest

from acc.server.config import Settings


@pytest.fixture(autouse=True)
def owner_controls_orchestrator(isolated_orchestrator_environment, monkeypatch):
    # These tests exercise owner choices, not a server-pinned legacy scenario.
    for key in ('ACC_ORCHESTRATOR', 'RYNHULT_ORCHESTRATOR'):
        monkeypatch.delenv(key, raising=False)


def test_defaults_match_existing_installation():
    from acc.server.admin_features import Features
    features = Features(Settings(), {})
    assert features.enabled('multirun')
    assert features.enabled('schedules')
    assert not features.enabled('containers')
    assert not features.enabled('organizations')


def test_database_choice_and_explicit_false_server_configuration(monkeypatch):
    from acc.env import environment
    from acc.server.admin_features import Features
    monkeypatch.delenv('ACC_ORGANIZATIONS', raising=False)
    monkeypatch.delenv('RYNHULT_ORGANIZATIONS', raising=False)
    assert Features(Settings(), {'organizations': True}).enabled('organizations')
    configured = Features(Settings(organizations_enabled=False), {'organizations': True})
    assert not configured.enabled('organizations')
    assert configured.locked('organizations')
    monkeypatch.setenv('ACC_ORGANIZATIONS', '1')
    monkeypatch.setenv('RYNHULT_ORGANIZATIONS', '0')
    assert environment.get('ACC_ORGANIZATIONS') == '0'
    assert not Features(Settings(), {'organizations': True}).enabled('organizations')


@pytest.mark.parametrize('key', ['sessions', 'terminal', 'inbox', 'backups', 'machines', 'guide', 'guardrails', 'sign_in'])
def test_core_is_not_a_feature_switch(key):
    from acc.server.admin_features import FEATURES
    assert key not in FEATURES


async def owner_browser(api):
    """Admin features change only from an owner's browser sign-in, never the installation API token."""
    if not api.cookies.get('acc_session'):
        from helpers import TOTP_AT
        from test_auth import ORIGIN, PASSWORD

        from acc.server import auth
        settings = api.app.state.ctx.settings
        token = settings.setup_token_path.read_text().strip()
        anon = {**ORIGIN, 'Authorization': ''}
        begin = await api.post('/api/auth/setup/begin', json={'setup_token': token}, headers=anon)
        assert begin.status_code == 200, begin.text
        done = await api.post('/api/auth/setup/complete', headers=anon, json={
            'setup_token': token, 'username': 'casey', 'password': PASSWORD,
            'code': auth.totp(begin.json()['totp_secret'], TOTP_AT)})
        assert done.status_code == 200, done.text
    return {'Authorization': '', 'Origin': api.app.state.ctx.settings.allowed_origins[0],
            'X-ACC-CSRF': api.cookies.get('acc_csrf') or ''}


async def change(api, key, enabled, revision=None, headers=None):
    if revision is None:
        state = await api.get('/api/admin-features')
        assert state.status_code == 200, state.text
        revision = state.json()['revision']
    headers = headers or await owner_browser(api)
    return await api.put(f'/api/admin-features/{key}', json={'enabled': enabled, 'revision': revision}, headers=headers)


@pytest.mark.parametrize(('key', 'path'), [
    ('multirun', '/api/multiruns'), ('schedules', '/api/schedules'),
    ('automation', '/api/sessions/none/automation'), ('live_preview', '/api/projects/1/preview'),
    ('publishing', '/api/publish'), ('issue_sources', '/api/issue-servers'),
    ('groups', '/api/groups'), ('teams', '/api/sessions/none/team'),
    ('containers', '/api/projects/1/containers'), ('orchestrator', '/api/gates'),
    ('remote_access', '/api/remote-access'), ('telemetry', '/api/telemetry'),
    ('mobile', '/api/mobile/v1/config'), ('local_models', '/api/endpoints'),
    ('notifications', '/api/push'), ('risk_labels', '/api/preferences/risk-labels'),
    ('explanations', '/api/sessions/none/explanation'), ('organizations', '/api/organizations/oidc'),
])
async def test_disabled_api_families_are_refused_and_audited(api, env, monkeypatch, key, path):
    from sqlalchemy import select

    from acc.server.models import AuditLog
    if key == 'remote_access':
        from unittest.mock import AsyncMock

        from acc.server import remote_access
        monkeypatch.setattr(remote_access, 'request', AsyncMock(return_value={'enabled': False, 'last_reload_result': 'verified'}))
    response = await change(api, key, False)
    assert response.status_code == 200, response.text
    response = await api.post(path, json={'enabled': True}) if key == 'containers' else await api.get(path)
    assert response.status_code == (409 if key == 'containers' else 404), response.text
    assert 'Admin features' in response.json()['detail']
    with api.app.state.db() as db:
        row = db.scalar(select(AuditLog).where(AuditLog.action == 'admin_features.update', AuditLog.target == key))
        assert row.detail['enabled'] is False


async def test_stale_tabs_cannot_overwrite_a_change_and_core_remains_available(api):
    first = await api.get('/api/admin-features')
    revision = first.json()['revision']
    assert (await change(api, 'multirun', False, revision)).status_code == 200
    assert (await change(api, 'multirun', True, revision)).status_code == 409
    assert (await api.get('/api/multiruns')).status_code == 404
    assert (await api.get('/api/sessions')).status_code == 200
    assert (await change(api, 'multirun', True)).status_code == 200
    assert (await api.get('/api/multiruns')).status_code == 200
    assert (await change(api, 'backups', False)).status_code == 404


async def test_sensitive_enable_requires_real_recent_browser_verification(api):
    response = await change(api, 'organizations', True)
    assert response.status_code == 403, response.text
    assert 'Verify your sign-in' in response.text


async def test_configured_feature_is_locked_even_when_false(api, monkeypatch):
    monkeypatch.setenv('RYNHULT_FEATURE_MULTIRUN', '0')
    response = await change(api, 'multirun', True)
    assert response.status_code == 409
    assert "server's configuration" in response.text


async def test_frozen_automation_and_schedule_dispatch_keep_data(api, env):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from acc.server.admin_features import Features, RuntimeSettings
    from acc.server.schedules import Scheduler
    features = Features(env['settings'], {'automation': False, 'schedules': False})
    settings = RuntimeSettings(env['settings'], features)
    scheduler = Scheduler(SimpleNamespace(settings=settings))
    scheduler.recover = AsyncMock()
    scheduler.dispatch = AsyncMock()
    await scheduler.tick()
    scheduler.recover.assert_not_awaited()
    from acc.server.reconcile import ReconcileMixin
    # Check the real bound supervisor method without touching a broker or session.
    supervisor = SimpleNamespace(settings=settings)
    await ReconcileMixin._check_automation(supervisor, None, None)
    await ReconcileMixin._attempt_resume(supervisor, None, None, None)


async def test_owner_browser_verification_enables_preview_without_bypassing_qualification(api, env, monkeypatch, project):
    from test_passkeys import setup
    headers, _ = await setup(api, env, monkeypatch)
    response = await change(api, 'containers', True, headers=headers)
    assert response.status_code == 200, response.text
    response = await api.post(f"/api/projects/{project['id']}/containers", json={'enabled': True}, headers=headers)
    assert response.status_code == 409 and 'qualified host' in response.text


async def test_member_and_agent_cannot_change_or_inspect_owner_policy(api, env, member_accounts):
    from helpers import verified_post
    from test_auth import do_setup
    from test_users import browser, csrf, join
    await do_setup(api, env)
    api.headers.pop('Authorization', None)
    invite = await verified_post(api, '/api/users/invites', {'role': 'member', 'account': 'dana-agent'})
    assert invite.status_code == 201, invite.text
    async with browser(api) as member:
        assert (await join(member, invite.json()['link'], 'taylor')).status_code == 200
        assert (await member.get('/api/admin-features')).status_code == 403
        assert (await member.put('/api/admin-features/multirun', headers=csrf(member),
                                 json={'enabled': False, 'revision': 0})).status_code == 403


async def test_team_off_stops_grant_and_retains_records(api, project, env):
    from test_team import lead

    from acc.server.models import Team
    team = await lead(api, project, env)
    response = await change(api, 'teams', False)
    assert response.status_code == 200, response.text
    with api.app.state.db() as db:
        assert db.get(Team, team['id']).stopped
    assert (await change(api, 'teams', True)).status_code == 200
    # Re-enabling availability must not resurrect a stopped team's execution grant.
    with api.app.state.db() as db:
        assert db.get(Team, team['id']).stopped


async def test_database_orchestrator_switch_is_live_without_restart(api, env, monkeypatch):
    from test_passkeys import setup

    from acc.server import orchestrator
    from acc.server.admin_features import CURRENT
    for name in ('ACC_ORCHESTRATOR', 'RYNHULT_ORCHESTRATOR', 'ACC_ORCHESTRATOR_V2', 'RYNHULT_ORCHESTRATOR_V2'):
        monkeypatch.delenv(name, raising=False)
    headers, _ = await setup(api, env, monkeypatch)
    assert (await change(api, 'orchestrator', True, headers=headers)).status_code == 200
    assert (await api.get('/api/gates', headers=headers)).status_code == 200
    token = CURRENT.set(api.app.state.ctx.settings.features)
    try:
        assert orchestrator.worklist.enabled()
        assert 'worklist' in orchestrator.team_fields()
        assert (await change(api, 'orchestrator', False, headers=headers)).status_code == 200
        assert not orchestrator.team_fields()
        assert (await api.get('/api/gates', headers=headers)).status_code == 404
    finally:
        CURRENT.reset(token)


def test_explicit_orchestrator_master_enables_unset_slice_flags(monkeypatch):
    from acc.server.admin_features import CURRENT, Features
    from acc.server.orchestrator.switch import slice_enabled
    monkeypatch.setenv('ACC_ORCHESTRATOR', '1')
    monkeypatch.delenv('ACC_ORCHESTRATOR_V2', raising=False)
    monkeypatch.delenv('RYNHULT_ORCHESTRATOR_V2', raising=False)
    token = CURRENT.set(Features(Settings()))
    try:
        assert slice_enabled('ACC_ORCHESTRATOR_V2')
    finally:
        CURRENT.reset(token)


async def test_public_off_closes_existing_streams_even_without_helper(api, monkeypatch):
    from unittest.mock import AsyncMock

    from acc.server import edge, remote_access
    connection = edge.Connection('synthetic-leaf')
    api.app.state.ctx.edge.open.add(connection)
    monkeypatch.setattr(remote_access, 'request', AsyncMock(side_effect=OSError('synthetic unavailable helper')))
    response = await change(api, 'remote_access', False)
    assert response.status_code == 200, response.text
    assert connection.closed.is_set()
    assert api.app.state.ctx.edge.check('synthetic-leaf', '/api/sessions') is None


async def test_telemetry_feature_off_pauses_sharing_but_still_sends_deletions(api, monkeypatch):
    import time

    from acc.server.models import TelemetryItem
    telemetry = api.app.state.ctx.supervisor.telemetry
    with api.app.state.db() as db:
        for kind in ('usage', 'delete'):
            db.add(TelemetryItem(id=kind, kind=kind, key_id='k' * 22, body='{}', state='approved',
                                 created=time.time(), due=time.time()))
        db.commit()
    assert (await change(api, 'telemetry', False)).status_code == 200
    sent = []

    async def record(s, item):
        sent.append(item.kind)
    monkeypatch.setattr(telemetry, 'send', record)
    await telemetry._tick()
    assert sent == ['delete']  # erasure of data already sent is never paused
    with api.app.state.db() as db:
        assert db.get(TelemetryItem, 'usage') is not None  # the paused queue is kept, not consent-off deleted
    # Off refuses the telemetry API except deleting data already sent.
    assert (await api.get('/api/telemetry')).status_code == 404
    assert (await api.post('/api/telemetry/delete')).status_code == 200


async def test_api_token_cannot_change_admin_features(api):
    revision = (await api.get('/api/admin-features')).json()['revision']
    response = await api.put('/api/admin-features/multirun', json={'enabled': False, 'revision': revision})
    assert response.status_code == 403 and 'API token' in response.text
    assert (await api.get('/api/multiruns')).status_code == 200


@pytest.mark.parametrize('key', ['mobile', 'groups_remote', 'organizations', 'containers', 'orchestrator'])
async def test_trust_widening_features_need_fresh_verification(api, key):
    if key == 'orchestrator':
        assert (await change(api, key, False)).status_code == 200
    response = await change(api, key, True)
    assert response.status_code == 403 and 'Verify your sign-in' in response.text


@pytest.mark.parametrize(('key', 'path', 'method'), [
    ('live_preview', '/api/sessions/none/preview-element', 'post'),
    ('live_preview', '/api/sessions/none/preview-element/draft/send', 'post'),
    ('remote_access', '/api/device-pairing/claim', 'post'),
    ('orchestrator', '/api/gates/wrapup', 'put'),
    ('orchestrator', '/api/gates/projects/1/flakes', 'put'),
    ('orchestrator', '/api/gates/job/accept-retry', 'post'),
    ('orchestrator', '/api/providers/image-check', 'post'),
    ('orchestrator', '/api/projects/1/brief', 'post'),
    ('orchestrator', '/api/project-brief-references/1', 'post'),
    ('orchestrator', '/api/teams/lead/assignments', 'get'),
    ('orchestrator', '/api/teams/lead/attempts/attempt/fail', 'post'),
])
async def test_alternate_routes_of_a_feature_are_refused(api, monkeypatch, key, path, method):
    if key == 'remote_access':
        from unittest.mock import AsyncMock

        from acc.server import remote_access
        monkeypatch.setattr(remote_access, 'request', AsyncMock(return_value={'enabled': False, 'last_reload_result': 'verified'}))
    assert (await change(api, key, False)).status_code == 200
    response = await api.request(method.upper(), path, json={} if method != 'get' else None)
    assert response.status_code == 404 and 'Admin features' in response.text


async def test_groups_off_still_allows_revoking_grants(api):
    assert (await change(api, 'groups', False)).status_code == 200
    assert (await api.get('/api/groups')).status_code == 404
    for path in ('/api/groups/1/key-grants/1/revoke', '/api/group-machines/1/revoke',
                 '/api/groups/1/projects/1/remote-placements/1/revoke'):
        response = await api.post(path, json={})
        assert 'Admin features' not in response.text, path  # reaches the route's own checks


async def test_team_off_intent_survives_unavailable_stop_worker(api, project, env, monkeypatch):
    from unittest.mock import AsyncMock

    from test_team import lead

    from acc.server.models import Session, Team
    team = await lead(api, project, env)
    monkeypatch.setattr(api.app.state.supervisor, 'stop_team', AsyncMock(side_effect=OSError('synthetic offline machine')))
    response = await change(api, 'teams', False)
    assert response.status_code == 200
    assert response.json()['cleanup_pending']
    with api.app.state.db() as db:
        assert db.get(Team, team['id']).stopped
        session = db.get(Session, team['id'])
        assert session.desired_state == 'stopped'
        assert session.team_stop_pending or session.observed_state in ('stopped', 'idle', 'exited')


def test_deriving_settings_does_not_pin_unrelated_default_flags():
    from dataclasses import replace

    from acc.server.admin_features import Features, RuntimeSettings
    original = Settings()
    derived = replace(original, cookie_secure=False)
    assert not Features(derived).locked('organizations')
    assert Features(replace(derived, organizations_enabled=True)).locked('organizations')
    runtime = RuntimeSettings(original, Features(original, {'organizations': True}))
    copied = replace(runtime, cookie_secure=False)
    assert copied.organizations_enabled
    assert not copied.features.locked('organizations')


async def test_already_approved_publish_is_not_orphaned_by_availability_off(api, monkeypatch):
    from unittest.mock import AsyncMock

    from helpers import write_db

    from acc.server.models import PublishRequest
    publisher = api.app.state.publisher
    with write_db(api.app.state.db) as db:
        db.add(PublishRequest(id='pub-admin-accepted', action='publish', host='site.example.com', port=8100,
                             access='public', requested_by='casey', status='approved'))
        db.commit()
    effect = AsyncMock(return_value='synthetic applied')
    monkeypatch.setattr(publisher, '_publish', effect)
    assert (await change(api, 'publishing', False)).status_code == 200
    await publisher.execute('pub-admin-accepted')
    effect.assert_awaited_once()
    with api.app.state.db() as db:
        assert db.get(PublishRequest, 'pub-admin-accepted').status == 'applied'


async def test_issue_source_background_comments_pause_without_touching_saved_state():
    from types import SimpleNamespace
    from unittest.mock import Mock

    from acc.server.admin_features import Features, RuntimeSettings
    from acc.server.polling import PollingMixin
    original = Settings()
    worker = SimpleNamespace(settings=RuntimeSettings(original, Features(original, {'issue_sources': False})),
                             db=Mock(side_effect=AssertionError('Off must preserve issue state without polling')))
    await PollingMixin.refresh_issue_comments(worker)
    await PollingMixin.comment_linear_pulls(worker, 1, [])
    worker.db.assert_not_called()
