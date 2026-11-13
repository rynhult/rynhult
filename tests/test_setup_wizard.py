"""Checklist saves are personal, revisioned data, never resource or policy actions."""
import pytest
from test_auth import cookie_client, do_setup
from test_users import browser, csrf, join


async def put(client, progress, **extra):
    return await client.put('/api/preferences/setup-level', headers=csrf(client), json={
        'level': 1, 'dismissed': True, 'wizard': progress, **extra})


async def test_resume_conflict_and_no_secret_storage(api, env):
    await do_setup(api, env)
    owner = cookie_client(api)
    assert (await put(owner, {'revision': 0, 'step': 'security', 'state': 'completed'})).status_code == 409
    assert (await put(owner, {'revision': 0, 'step': 'recovery', 'state': 'deferred'})).status_code == 422
    saved = await put(owner, {'revision': 0, 'step': 'recovery', 'state': 'completed'})
    assert saved.status_code == 200
    progress = saved.json()['wizard']
    assert progress['revision'] == 1
    assert progress['steps']['recovery']['updated_at'] > 0
    # Another browser resuming a stale write must reload and review, including duplicate submits.
    assert (await put(cookie_client(api), {'revision': 0, 'step': 'recovery', 'state': 'completed'})).status_code == 409
    assert (await cookie_client(api).get('/api/preferences/setup-level')).json()['wizard'] == progress
    assert (await put(owner, {'revision': 1, 'step': 'provider', 'state': 'completed', 'api_key': 'fake'})).status_code == 422
    # Ordinary level saves keep the entire wizard record.
    response = await owner.put('/api/preferences/setup-level', headers=csrf(owner), json={'level': 6})
    assert response.json()['wizard'] == progress


async def test_progress_owned_by_person_and_no_policy_or_feature_writes(api, env, member_accounts):
    await do_setup(api, env)
    owner = cookie_client(api)
    before = (await owner.get('/api/security/registry')).json()
    features = (await owner.get('/api/admin-features')).json()
    invite = await owner.post('/api/users/invites', headers=csrf(owner), json={'account': 'dana-agent'})
    async with browser(api) as member:
        assert (await join(member, invite.json()['link'], 'alex')).status_code == 200
        assert (await put(member, {'revision': 0, 'step': 'recovery', 'state': 'completed'})).status_code == 200
        for revision, step in enumerate(('level', 'security', 'machine', 'provider', 'project', 'backups', 'updates', 'features'), 1):
            response = await put(member, {'revision': revision, 'step': step,
                                         'state': 'completed' if step == 'level' else 'deferred'})
            assert response.status_code == 200
        finished = await put(member, {'revision': 9, 'step': 'finish', 'state': 'completed'})
        assert finished.status_code == 200
        assert finished.json()['wizard']['steps']['backups']['state'] == 'deferred'
        assert (await owner.get('/api/preferences/setup-level')).json()['wizard']['revision'] == 0
        # Unknown actor/scope fields cannot redirect a personal write.
        assert (await put(member, {'revision': 10, 'step': 'finish', 'state': 'completed', 'username': 'casey'})).status_code == 422
    assert (await owner.get('/api/security/registry')).json() == before
    assert (await owner.get('/api/admin-features')).json() == features


async def test_interrupted_finish_requires_explicit_steps(api, env):
    await do_setup(api, env)
    owner = cookie_client(api)
    await put(owner, {'revision': 0, 'step': 'recovery', 'state': 'completed'})
    assert (await put(owner, {'revision': 1, 'step': 'finish', 'state': 'completed'})).status_code == 409
    # References have no authority and never cause creation/replay.
    saved = await put(owner, {'revision': 1, 'step': 'machine', 'state': 'pending',
                              'binding': {'machine': 'removed-node', 'account': 'example-agent'}, 'project_id': 987654})
    assert saved.status_code == 200
    assert saved.json()['wizard']['binding']['machine'] == 'removed-node'
    assert (await owner.get('/api/projects')).json() == []


@pytest.mark.parametrize('field,value', [('revision', True), ('revision', -1), ('step', 'secret'),
                                       ('state', 'ready'), ('project_id', '1'), ('password', 'fake')])
async def test_invalid_progress_refused(api, env, field, value):
    await do_setup(api, env)
    body = {'revision': 0, 'step': 'recovery', 'state': 'completed', field: value}
    assert (await put(cookie_client(api), body)).status_code == 422


def test_all_level_recommendations_match_live_security_registry():
    import re
    from pathlib import Path

    from acc.server.security_settings import REGISTRY

    source = (Path(__file__).resolve().parents[1] / 'web/src/lib/setupLevels.ts').read_text()
    recommendations = source.split('export const SECURITY_DEFAULTS = {', 1)[1].split('};', 1)[0]
    assert set(re.findall(r'(\w+): true', recommendations)) == set(REGISTRY)
    assert 'false' not in recommendations
    # All seven levels use this one recommendation set; none has a weaker row.
    assert re.findall(r'\{ id: (\d), name:', source) == list('0123456')


async def test_wizard_refuses_installation_token_and_requires_csrf(api, env):
    assert (await api.get('/api/preferences/setup-level')).status_code == 403
    assert (await api.put('/api/preferences/setup-level', json={'level': 1, 'wizard': {
        'revision': 0, 'step': 'recovery', 'state': 'completed'}})).status_code == 403
    await do_setup(api, env)
    owner = cookie_client(api)
    assert (await owner.put('/api/preferences/setup-level', json={'level': 1, 'wizard': {
        'revision': 0, 'step': 'recovery', 'state': 'completed'}})).status_code == 403


async def test_existing_person_can_defer_recovery_without_claiming_codes_saved(api, env):
    from acc.server.models import Setting

    await do_setup(api, env)
    # Existing installations have no fresh-enrollment record.
    with api.app.state.db() as db:
        db.delete(db.get(Setting, 'setup_level.casey'))
        db.commit()
    owner = cookie_client(api)
    saved = await put(owner, {'revision': 0, 'step': 'recovery', 'state': 'deferred'})
    assert saved.status_code == 200
    assert saved.json()['wizard']['steps']['recovery']['state'] == 'deferred'
    assert (await put(owner, {'revision': 1, 'step': 'level', 'state': 'completed'})).status_code == 200
