"""Lane D: person-owned scope, dependency gates and prepare-only launch authority."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_orchestrator_planning import PHASES, SCOPE, plan  # noqa: F401 (fixture)
from test_orchestrator_planning import flag as flag
from test_orchestrator_selection_api import assigned_start
from test_team import grant, lead, repo, request

from acc.server.compiler import LaunchInputs
from acc.server.orchestrator import delivery, planning, worklist
from acc.server.supervisor_common import LaunchRefused


@pytest.fixture
async def on(flag, api, env):  # noqa: F811 (fixture)
    # Person-owned boundaries need a signed-in person, never the installation token (review L-D2).
    from test_auth import cookie_client, do_setup
    from test_users import csrf
    await do_setup(api, env)
    cookie_client(api)
    api.headers.update(csrf(api))
    return api


async def test_dropped_prerequisite_remains_blocking_until_explicit_relink(on, project, env):
    x = await lead(on, project, env)
    result = await plan(on, env, x, idea='Import', scope=SCOPE, phases=PHASES)
    assert result['ok']
    result = await plan(on, env, x, phases=[{'key': 'schema', 'status': 'dropped'}], reason='No schema needed')
    assert result['ok']
    assert result['result']['phases'][1]['blocked_by'] == ['schema']
    result = await plan(on, env, x, phases=[{'key': 'importer', 'depends_on': []}], reason='Importer uses existing tables')
    assert result['ok'] and result['result']['phases'][1]['blocked_by'] == []


async def test_constraints_and_requirements_are_person_owned_even_after_lead_reads(on, project, env):
    x = await lead(on, project, env)
    url = f"/api/teams/{x['id']}/plan"
    person = await on.patch(url, json={'version': 0, 'reason': 'Product boundary',
        'constraints': [{'text': 'No cloud uploads'}],
        'requirements': [{'text': 'Import CSV', 'acceptance': 'CSV round trip works'}]})
    assert person.status_code == 200, person.text
    assert person.json()['constraints'][0]['id'] == 'C-1'
    assert person.json()['requirements'][0]['id'] == 'R-1'
    assert (await request(on, env, x, 'plan'))['ok']
    refused = await plan(on, env, x, constraints=[{'id': 'C-1', 'text': 'Cloud allowed'}])
    assert not refused['ok']
    refused = await plan(on, env, x, requirements=[{'id': 'R-1', 'text': 'Skip import'}])
    assert not refused['ok']
    proposed = await plan(on, env, x, propose_constraints=[{'id': 'C-1', 'text': 'Allow cloud exports'}])
    assert proposed['ok'], proposed
    current = (await on.get(url)).json()
    assert current['constraints'][0]['text'] == 'No cloud uploads'
    cards = (await on.get(f"/api/teams/{x['id']}/worklist")).json()['items']
    assert any(i['kind'] == 'decision' and 'C-1' in i['title'] for i in cards)
    card = next(i for i in cards if i['kind'] == 'decision')
    assert not (await request(on, env, x, 'worklist_update', item=card['id'], steps='Nothing changes'))['ok']
    assert (await on.patch(url, json={'version': 0, 'constraints': [{'text': 'stale'}]})).status_code == 409


@pytest.mark.parametrize('field,proposal_field', [
    ('constraints', 'propose_constraints'), ('requirements', 'propose_requirements'),
])
async def test_identical_boundary_proposal_flags_lead_without_requesting_approval(on, project, env, field, proposal_field):
    x = await lead(on, project, env)
    url = f"/api/teams/{x['id']}/plan"
    original = (await on.patch(url, json={'version': 0, field: [{'text': 'Keep notes private'}]})).json()[field][0]
    proposed = await plan(on, env, x, **{proposal_field: [{'id': original['id'], 'text': 'Keep notes private'}]})
    assert proposed['ok'], proposed
    assert 'No difference found between current and proposed' in proposed['result']['warnings'][0]
    cards = (await on.get(f"/api/teams/{x['id']}/worklist")).json()['items']
    card = next(i for i in cards if original['id'] in i['title'])
    assert card['kind'] == 'note' and card['answer'] is None and card['options'] is None
    assert 'No difference found between current and proposed' in card['steps']
    assert 'Acceptance:' not in card['steps']
    assert (await on.get(url)).json()[field][0] == original
    assert not (await on.patch(f"/api/teams/{x['id']}/worklist/items/{card['id']}",
        json={'revision': card['revision'], 'answer': 'accept'})).is_success


async def test_acceptance_only_proposal_is_a_real_change_and_new_boundary_has_no_current_text(on, project, env):
    x = await lead(on, project, env)
    url = f"/api/teams/{x['id']}/plan"
    assert (await on.patch(url, json={'version': 0, 'requirements': [
        {'text': 'Import CSV', 'acceptance': 'Accept valid files'}]})).is_success
    result = await plan(on, env, x, propose_requirements=[
        {'id': 'R-1', 'text': 'Import CSV', 'acceptance': 'Refuse invalid rows'}],
        propose_constraints=[{'text': 'No uploads'}])
    assert result['ok'], result
    cards = (await on.get(f"/api/teams/{x['id']}/worklist")).json()['items']
    requirement = next(i for i in cards if 'R-1' in i['title'])
    assert requirement['kind'] == 'decision'
    assert 'Current acceptance: Accept valid files' in requirement['steps']
    assert 'Proposed acceptance: Refuse invalid rows' in requirement['steps']
    constraint = next(i for i in cards if 'C-1' in i['title'])
    assert constraint['kind'] == 'decision'
    assert 'Current: No accepted boundary yet.' in constraint['steps']
    assert 'Proposed: No uploads' in constraint['steps']
    assert 'Acceptance:' not in constraint['steps']
    accepted = await on.patch(f"/api/teams/{x['id']}/worklist/items/{requirement['id']}",
        json={'revision': requirement['revision'], 'answer': 'accept'})
    assert accepted.is_success, accepted.text
    assert (await on.get(url)).json()['requirements'][0]['acceptance'] == 'Refuse invalid rows'


async def test_existing_identical_proposal_becomes_a_note_without_changing_the_boundary(on, project, env, monkeypatch):
    from acc.server import attention
    from acc.server.orchestrator import planning_controls as controls
    monkeypatch.setenv('RYNHULT_ORCHESTRATOR_V2_DELIVERY', '1')
    monkeypatch.setattr(delivery, 'wake', lambda supervisor: None)
    x = await lead(on, project, env)
    url = f"/api/teams/{x['id']}/plan"
    original = (await on.patch(url, json={'version': 0, 'constraints': [{'text': 'No uploads'}]})).json()['constraints'][0]
    with on.app.state.db() as db:
        card = worklist.add(db, x['id'], f"session:{x['id']}", 'lead', {
            'kind': 'decision', 'title': 'Proposed change to C-1',
            'steps': 'Current: No uploads\nProposed: No uploads\nAcceptance: ',
            'options': [{'id': 'accept', 'label': 'Accept change'}, {'id': 'keep', 'label': 'Keep current boundary'}]})
        db.add(controls.PlanProposal(item_id=card.id, team_id=x['id'], kind='C', target_id='C-1',
            base_version=1, text='No uploads', acceptance=''))
        item_id, revision = card.id, card.revision
        db.commit()
    cards = (await on.get(f"/api/teams/{x['id']}/worklist")).json()['items']
    current = next(i for i in cards if i['id'] == item_id)
    assert current['kind'] == 'note' and current['options'] is None and current['answer'] is None
    assert current['revision'] > revision
    assert 'No difference found between current and proposed' in current['steps']
    seen = await request(on, env, x, 'worklist')
    assert seen['ok'] and next(i for i in seen['result']['items'] if i['id'] == item_id)['kind'] == 'note'
    assert (await on.get(url)).json()['constraints'][0] == original
    assert (await on.patch(f"/api/teams/{x['id']}/worklist/items/{item_id}",
        json={'revision': revision, 'answer': 'accept'})).status_code == 409
    with on.app.state.db() as db:
        queued = list(db.scalars(select(delivery.MessageDelivery).where(delivery.MessageDelivery.session_id == x['id'])))
        notices = [row for row in queued if row.body.startswith(delivery.NUDGE_ORIGIN + 'No difference found')]
        assert attention._open(db, f'worklist:{item_id}') is None
        assert len(notices) == 1
        assert any(row.body.startswith(delivery.NUDGE_ORIGIN + 'your person edited the plan.') for row in queued)
        assert notices[0].actor == 'supervisor'
        assert notices[0].body.startswith(delivery.NUDGE_ORIGIN)
        assert 'No difference found between current and proposed' in notices[0].body
        assert 'your person' not in notices[0].body[len(delivery.NUDGE_ORIGIN):]
        assert 'No uploads' not in notices[0].body


@pytest.mark.parametrize('person_edit', [{'steps': 'Keep my explanation.'}, {'answer': 'keep'}])
async def test_proposal_refresh_preserves_person_edits_and_recorded_answers(on, project, env, person_edit):
    x = await lead(on, project, env)
    url = f"/api/teams/{x['id']}/plan"
    assert (await on.patch(url, json={'version': 0, 'constraints': [{'text': 'Local only'}]})).is_success
    assert (await plan(on, env, x, propose_constraints=[{'id': 'C-1', 'text': 'Invited people only'}]))['ok']
    card = next(i for i in (await on.get(f"/api/teams/{x['id']}/worklist")).json()['items'] if i['kind'] == 'decision')
    edited = await on.patch(f"/api/teams/{x['id']}/worklist/items/{card['id']}",
        json={'revision': card['revision'], **person_edit})
    assert edited.is_success, edited.text
    current = (await on.get(url)).json()
    assert (await on.patch(url, json={'version': current['version'],
        'constraints': [{'id': 'C-1', 'text': 'Invited people only'}]})).is_success
    refreshed = (await on.get(f"/api/teams/{x['id']}/worklist")).json()['items']
    assert next(i for i in refreshed if i['id'] == card['id']) == edited.json()


async def test_stale_legacy_proposal_loses_empty_acceptance_without_rebinding_its_current_text(on, project, env):
    x = await lead(on, project, env)
    url = f"/api/teams/{x['id']}/plan"
    assert (await on.patch(url, json={'version': 0, 'constraints': [{'text': 'Local only'}]})).is_success
    assert (await plan(on, env, x, propose_constraints=[{'id': 'C-1', 'text': 'Cloud allowed'}]))['ok']
    with on.app.state.db() as db:
        card = db.scalar(select(worklist.WorkItem).where(worklist.WorkItem.team_id == x['id'], worklist.WorkItem.kind == 'decision'))
        card.steps = 'Current: Local only\nProposed: Cloud allowed\nAcceptance: '
        item_id = card.id
        db.commit()
    current = (await on.get(url)).json()
    assert (await on.patch(url, json={'version': current['version'],
        'constraints': [{'id': 'C-1', 'text': 'Offline only'}]})).is_success
    card = next(i for i in (await on.get(f"/api/teams/{x['id']}/worklist")).json()['items'] if i['id'] == item_id)
    assert card['steps'] == 'Current: Local only\nProposed: Cloud allowed'
    refused = await on.patch(f"/api/teams/{x['id']}/worklist/items/{item_id}",
        json={'revision': card['revision'], 'answer': 'accept'})
    assert refused.status_code == 409 and 'boundary changed' in refused.json()['detail']
    assert (await on.get(url)).json()['constraints'][0]['text'] == 'Offline only'


@pytest.mark.parametrize('provider,field,value', [('claude', 'permission_mode', 'plan'),
    ('codex', 'sandbox', 'read-only'), ('copilot', 'mode', 'plan')])
async def test_prepare_lead_launch_has_provider_read_only_permission(on, project, env, provider, field, value):
    repo(env['workspaces'] / 'demo')
    response = await on.post('/api/sessions', json={'project_id': project['id'], 'name': 'Prepare',
        'provider': provider, 'worktree': True, 'orchestrator': grant(run_mode='prepare')})
    assert response.status_code == 201, response.text
    session = response.json()
    preview = await on.post('/api/sessions/preview', json={'project_id': project['id'], 'name': 'Prepare',
        'provider': provider, 'worktree': True, 'orchestrator': grant(run_mode='prepare')})
    assert preview.status_code == 200, preview.text
    inputs = session['launch_spec']['inputs']
    actual = inputs.get('options', {}).get(field, inputs.get(field))
    assert actual == value, inputs
    from acc import providers
    argv = providers.get(provider).args(LaunchInputs.from_dict(inputs), run_dir='/tmp/fixture',
        tmux_name='fixture', native_id=session['native_id'], resume=False,
        files=session['launch_spec']['files'], extra=[])
    permission_flag = {'claude': '--permission-mode', 'codex': '--sandbox', 'copilot': '--mode'}[provider]
    assert argv[argv.index(permission_flag) + 1] == value
    preview_args = preview.json()['command']
    assert preview_args[preview_args.index(permission_flag) + 1] == value
    assert '--allow-all' not in argv and '--allow-all-tools' not in argv


async def test_material_cards_block_only_linked_work_and_defaults_are_assumed(on, project, env):
    x = await lead(on, project, env)
    url = f"/api/teams/{x['id']}/plan"
    p = await on.patch(url, json={'version': 0, 'requirements': [
        {'text': 'Import CSV'}, {'text': 'Export CSV'}]})
    assert p.status_code == 200
    assert (await plan(on, env, x, idea='CSV', scope=SCOPE, phases=[
        {'key': 'import', 'title': 'Import', 'requirement_ids': ['R-1']},
        {'key': 'export', 'title': 'Export', 'requirement_ids': ['R-2']}]))['ok']
    asked = await request(on, env, x, 'worklist_add', kind='decision', title='Approve import privacy',
        options=[{'id': 'yes', 'label': 'Approve'}, {'id': 'no', 'label': 'Decline'}], blocks=['import', 'R-1'],
        default={'option': 'yes'})
    assert asked['ok'], asked
    assert asked['result']['in_effect'] is None  # a material block is not bypassed by declaring a default
    card = asked['result']
    with on.app.state.supervisor.db() as db:
        assignment = SimpleNamespace(team_id=x['id'], task_type='implementation', requirement_ids=['R-1'], need={})
        with pytest.raises(LaunchRefused, match='Approve import privacy'):
            planning.check_assignment(db, x['id'], assignment, LaunchInputs(provider='claude'))
        assignment.requirement_ids = ['R-2']
        planning.check_assignment(db, x['id'], assignment, LaunchInputs(provider='claude'))
    assert not (await request(on, env, x, 'worklist_update', item=card['id'], blocks=[]))['ok']
    assert (await on.patch(f"/api/teams/{x['id']}/worklist/items/{card['id']}",
        json={'revision': card['revision'], 'answer': 'yes'})).status_code == 200
    with on.app.state.supervisor.db() as db:
        assignment.requirement_ids = ['R-1']
        planning.check_assignment(db, x['id'], assignment, LaunchInputs(provider='claude'))
    assert not (await request(on, env, x, 'worklist_add', kind='question', title='Unknown', blocks=['missing']))['ok']


async def test_prepare_checks_discovery_and_frozen_permission_and_person_changes_mode(on, project, env):
    x = await lead(on, project, env, run_mode='prepare')
    with on.app.state.supervisor.db() as db:
        assignment = SimpleNamespace(team_id=x['id'], task_type='implementation', requirement_ids=[], need={})
        ro = LaunchInputs(provider='claude', permission_mode='plan')
        with pytest.raises(LaunchRefused, match='read-only discovery'):
            planning.check_assignment(db, x['id'], assignment, ro)
        assignment.task_type = 'discovery'
        with pytest.raises(LaunchRefused, match='read-only discovery'):
            planning.check_assignment(db, x['id'], assignment, LaunchInputs(provider='claude'))
        planning.check_assignment(db, x['id'], assignment, ro)
    url = f"/api/teams/{x['id']}/plan"
    assert (await on.patch(url, json={'version': 0, 'run_mode': 'build'})).status_code == 409
    await on.app.state.supervisor.stop(x['id'], 'owner')
    response = await on.patch(url, json={'version': 0, 'run_mode': 'build', 'reason': 'Discovery complete'})
    assert response.status_code == 200, response.text
    assert response.json()['run_mode'] == 'build'
    await on.app.state.supervisor.resume(x['id'], 'owner')
    narrowed = await plan(on, env, x, run_mode='prepare', reason='More discovery needed')
    assert narrowed['ok'], narrowed
    assert (await on.get(url)).json()['run_mode'] == 'prepare'


async def test_person_widens_a_pinned_prepare_brief_and_implementation_uses_the_effective_plan(on, project, env, monkeypatch):
    from acc.server.orchestrator import project_briefs

    assert (await on.post(f'/api/projects/{project["id"]}/brief', json={
        'version': 0, 'fields': {'product': 'Private bookings', 'run_mode': 'prepare'}})).status_code == 200
    x = await lead(on, project, env, run_mode='prepare')
    url = f"/api/teams/{x['id']}/plan"
    initial = (await on.get(url)).json()
    await on.app.state.supervisor.stop(x['id'], 'casey')
    changed = await on.patch(url, json={'version': initial['version'], 'run_mode': 'build', 'reason': 'Discovery accepted'})
    assert changed.status_code == 200, changed.text
    assert changed.json()['history'][0]['actor'] == 'casey'
    assert changed.json()['history'][0]['reason'] == 'Discovery accepted'
    assert changed.json()['run_mode'] == 'build'
    brief = (await on.get(f"/api/teams/{x['id']}/brief")).json()
    assert brief['run_mode'] == 'build'
    assert brief['suggested_run_mode'] == 'prepare'
    assert brief['submitted']['run_mode'] == 'prepare'
    with on.app.state.db() as db:
        assert db.get(project_briefs.BriefVersion, (project['id'], 1)).submitted['run_mode'] == 'prepare'
    await on.app.state.supervisor.resume(x['id'], 'casey')
    assert (await on.get(f"/api/sessions/{x['id']}")).json()['launch_spec']['inputs']['permission_mode'] == 'acceptEdits'
    assert (await request(on, env, x, 'brief_interpret', brief_version=1, text='Bounded bookings', references={}))['ok']
    drafted = await request(on, env, x, 'brief_draft', brief_version=1,
                            requirements=[{'text': 'Book privately', 'acceptance': 'Bookings stay private'}], constraints=[])
    assert drafted['ok'], drafted
    card = next(i for i in (await on.get(f"/api/teams/{x['id']}/worklist")).json()['items'] if i['kind'] == 'decision')
    assert (await on.patch(f"/api/teams/{x['id']}/worklist/items/{card['id']}",
                          json={'revision': card['revision'], 'answer': 'accept'})).status_code == 200
    assert (await request(on, env, x, 'plan'))['ok']
    assert (await plan(on, env, x, idea='Private bookings', scope=SCOPE,
                       phases=[{'key': 'booking', 'title': 'Book privately', 'requirement_ids': ['R-1']}]))['ok']
    assert (await request(on, env, x, 'plan_ready', reason='Person accepted the scope'))['ok']
    assignment, selected = await real_assignment(on, env, x, ['R-1'], monkeypatch)
    started = await request(on, env, x, 'start', name='Builder', assignment=assignment, selection=selected)
    assert started['ok'], started


async def test_lead_cannot_widen_prepare_and_the_refusal_points_to_the_persons_control(on, project, env):
    x = await lead(on, project, env, run_mode='prepare')
    refused = await request(on, env, x, 'plan_update', run_mode='build', reason='Start implementing')
    assert not refused['ok']
    assert 'person' in refused['error'] and 'Run mode' in refused['error']
    current = (await on.get(f"/api/teams/{x['id']}/plan")).json()
    assert current['run_mode'] == 'prepare' and current['version'] == 0


@pytest.mark.parametrize('before,after,permission', [('prepare', 'build', 'acceptEdits'), ('build', 'prepare', 'plan')])
async def test_person_mode_switch_queues_a_versioned_continuation_for_resume(on, project, env, monkeypatch, before, after, permission):
    monkeypatch.setenv('ACC_ORCHESTRATOR_V2_DELIVERY', '1')
    x = await lead(on, project, env, run_mode=before)
    url = f"/api/teams/{x['id']}/plan"
    await on.post(f"/api/sessions/{x['id']}/stop")
    response = await on.patch(url, json={'version': 0, 'run_mode': after})
    assert response.status_code == 200, response.text
    notice = response.json()['run_mode_delivery']
    await delivery.process_once(on.app.state.supervisor)
    with on.app.state.db() as db:
        row = db.get(delivery.MessageDelivery, notice['id'])
        assert row.state == 'queued' and row.session_id == x['id']
        assert f"Plan version {response.json()['version']}" in row.body
        assert f'Run mode is now {after}' in row.body
        assert ('plan_ready' in row.body and 'unblocked phases' in row.body) if after == 'build' else 'read-only' in row.body
    resumed = await on.post(f"/api/sessions/{x['id']}/resume")
    assert resumed.status_code == 200, resumed.text
    current = (await on.get(f"/api/sessions/{x['id']}")).json()
    assert current['launch_spec']['inputs']['permission_mode'] == permission
    assert resumed.json()['desired_state'] == 'running'
    assert (await on.get(url)).json()['history'][0]['actor'] == 'casey'


async def test_build_resume_restores_auto_in_the_leads_settings_and_command(on, project, env):
    from helpers import wait_for

    from acc import providers

    repo(env['workspaces'] / 'demo')
    created = await on.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude',
        'name': 'Auto permission lead', 'permission_mode': 'auto', 'worktree': True,
        'orchestrator': grant(run_mode='prepare')})
    assert created.status_code == 201, created.text
    lead = created.json()

    async def running_generation(count):
        async def running():
            current = (await on.get(f"/api/sessions/{lead['id']}")).json()
            runs = current['runs']
            return (len(runs) == count and runs[-1]['verified_at'] and not runs[-1]['ended_at']
                    and current['observed_state'] == 'idle')
        await wait_for(running)

    # A queued SessionStart from the stopped run can report idle before resume is dispatched.
    await running_generation(1)
    for version, mode, permission in [(0, 'build', 'auto'), (1, 'prepare', 'plan'), (2, 'build', 'auto')]:
        assert (await on.post(f"/api/sessions/{lead['id']}/stop")).status_code == 200
        changed = await on.patch(f"/api/teams/{lead['id']}/plan", json={'version': version, 'run_mode': mode})
        assert changed.status_code == 200, changed.text
        assert (await on.post(f"/api/sessions/{lead['id']}/resume")).status_code == 200
        current = (await on.get(f"/api/sessions/{lead['id']}")).json()
        spec = current['launch_spec']
        assert spec['inputs']['permission_mode'] == permission
        assert json.loads(spec['files']['settings.json'])['permissions']['defaultMode'] == permission
        args = providers.get('claude').args(LaunchInputs.from_dict(spec['inputs']), run_dir='/tmp/fixture',
            tmux_name='fixture', native_id=current['native_id'], resume=True, files=spec['files'], extra=[])
        assert args[args.index('--permission-mode') + 1] == permission
        assert '--resume' in args
        await running_generation(version + 2)
        settings = json.loads((Path(env['run_dir']) / lead['id'] / 'settings.json').read_text())
        assert settings['permissions']['defaultMode'] == permission


async def test_a_new_mode_notice_supersedes_an_undelivered_old_mode(on, project, env, monkeypatch):
    monkeypatch.setenv('ACC_ORCHESTRATOR_V2_DELIVERY', '1')
    x = await lead(on, project, env, run_mode='prepare')
    await on.post(f"/api/sessions/{x['id']}/stop")
    url = f"/api/teams/{x['id']}/plan"
    first = await on.patch(url, json={'version': 0, 'run_mode': 'build'})
    second = await on.patch(url, json={'version': 1, 'run_mode': 'prepare'})
    assert first.status_code == second.status_code == 200
    with on.app.state.db() as db:
        old = db.get(delivery.MessageDelivery, first.json()['run_mode_delivery']['id'])
        assert old.state == 'expired' and old.body == '' and old.error == 'superseded_run_mode'
        latest = db.get(delivery.MessageDelivery, second.json()['run_mode_delivery']['id'])
        assert latest.state == 'queued' and 'Run mode is now prepare' in latest.body


async def test_mode_and_permissions_roll_back_if_its_continuation_cannot_be_queued(on, project, env, monkeypatch):
    monkeypatch.setenv('ACC_ORCHESTRATOR_V2_DELIVERY', '1')
    x = await lead(on, project, env, run_mode='prepare')
    await on.post(f"/api/sessions/{x['id']}/stop")
    sup = on.app.state.supervisor
    with sup.db() as db:
        for _ in range(20):
            delivery.enqueue_in_transaction(db, sup, db.get(worklist.Session, x['id']), 'casey', 'An existing queued message.')
        db.commit()
    response = await on.patch(f"/api/teams/{x['id']}/plan", json={'version': 0, 'run_mode': 'build'})
    assert response.status_code == 409 and 'queue is full' in response.text
    current = (await on.get(f"/api/teams/{x['id']}/plan")).json()
    assert current['run_mode'] == 'prepare' and current['version'] == 0
    session = (await on.get(f"/api/sessions/{x['id']}")).json()
    assert session['launch_spec']['inputs']['permission_mode'] == 'plan' and session['desired_state'] == 'stopped'


async def test_lead_narrows_and_stops_before_resuming_with_read_only_permissions(on, project, env):
    x = await lead(on, project, env)
    narrowed = await request(on, env, x, 'plan_update', run_mode='prepare', reason='Need more discovery')
    assert narrowed['ok'], narrowed
    current = (await on.get(f"/api/teams/{x['id']}/plan")).json()
    assert current['run_mode'] == 'prepare'
    assert current['history'][0]['actor'] == f"session:{x['id']}"
    assert current['history'][0]['reason'] == 'Need more discovery'
    session = (await on.get(f"/api/sessions/{x['id']}")).json()
    assert session['desired_state'] == 'stopped' and not session['team_stop_pending']
    assert session['launch_spec']['inputs']['permission_mode'] == 'plan'
    await on.app.state.supervisor.resume(x['id'], 'casey')
    assert (await on.get(f"/api/sessions/{x['id']}")).json()['launch_spec']['inputs']['permission_mode'] == 'plan'


async def test_lead_cannot_narrow_while_a_write_helper_is_still_running(on, project, env):
    x = await lead(on, project, env)
    assert (await plan(on, env, x, idea='CSV', scope=SCOPE, phases=PHASES[:1]))['ok']
    assert (await request(on, env, x, 'plan_ready', reason='Ready'))['ok']
    helper = (await assigned_start(on, env, x, name='Builder', task='Build'))['result']
    refused = await request(on, env, x, 'plan_update', run_mode='prepare', reason='Need more discovery')
    assert not refused['ok'] and 'stop the helpers' in refused['error']
    assert (await on.get(f"/api/teams/{x['id']}/plan")).json()['run_mode'] == 'build'
    assert (await on.get(f"/api/sessions/{x['id']}")).json()['desired_state'] == 'running'
    await on.app.state.supervisor.stop(helper['id'], 'casey')
    assert (await request(on, env, x, 'plan_update', run_mode='prepare', reason='Helpers are stopped'))['ok']
    assert (await on.get(f"/api/teams/{x['id']}/plan")).json()['run_mode'] == 'prepare'


@pytest.mark.parametrize('actor', ['system', 'supervisor', 'schedule:example', 'session:other'])
async def test_non_human_principals_cannot_widen_a_stopped_prepare_plan(on, project, env, actor):
    from acc.server.orchestrator import planning_controls

    x = await lead(on, project, env, run_mode='prepare')
    await on.app.state.supervisor.stop(x['id'], 'casey')
    with on.app.state.db() as db:
        with pytest.raises(worklist.Refused, match='only the person'):
            planning_controls.set_run_mode(db, x['id'], actor, 'build')
        db.rollback()
    assert (await on.get(f"/api/teams/{x['id']}/plan")).json()['run_mode'] == 'prepare'


async def test_installation_token_is_not_a_signed_in_human_for_mode_changes(on, project, env):
    x = await lead(on, project, env, run_mode='prepare')
    await on.app.state.supervisor.stop(x['id'], 'casey')
    response = await on.patch(f"/api/teams/{x['id']}/plan",
        headers={'Authorization': f"Bearer {env['settings'].api_token()}"},
        json={'version': 0, 'run_mode': 'build'})
    assert response.status_code == 403, response.text
    assert (await on.get(f"/api/teams/{x['id']}/plan")).json()['run_mode'] == 'prepare'


async def test_brief_drafts_are_proposals_and_person_acceptance_is_version_bound(on, project, env):
    x = await lead(on, project, env)
    with on.app.state.supervisor.db() as db:
        out = planning.draft_brief(db, x['id'], 'casey', 1,
            [{'text': 'CSV import', 'acceptance': ['Accept a valid file', 'Refuse invalid rows']}], ['No external uploads'])
        assert out['constraints'][0]['state'] == 'active' and out['constraints'][0]['source'] == 'brief'
        assert out['constraints'][0]['brief_version'] == 1
        draft = planning.draft_brief(db, x['id'], f"session:{x['id']}", 2, [], ['Use cloud storage'])
        assert draft['constraints'][1]['state'] == 'proposed_change'
        db.commit()
    cards = (await on.get(f"/api/teams/{x['id']}/worklist")).json()['items']
    card = next(i for i in cards if i['kind'] == 'decision')
    accepted = await on.patch(f"/api/teams/{x['id']}/worklist/items/{card['id']}",
        json={'revision': card['revision'], 'answer': 'accept'})
    assert accepted.status_code == 200, accepted.text
    current = (await on.get(f"/api/teams/{x['id']}/plan")).json()
    assert current['constraints'][1]['state'] == 'active'
    assert current['constraints'][1]['version'] == 2


async def test_changes_invalidate_only_affected_evidence_and_deliver_versioned_notices(on, project, env, monkeypatch):
    from acc.server.orchestrator import planning_controls as controls
    x = await lead(on, project, env)
    assert (await plan(on, env, x, idea='CSV', scope=SCOPE, phases=PHASES[:1]))['ok']
    assert (await request(on, env, x, 'plan_ready', reason='Ready'))['ok']
    helper = (await assigned_start(on, env, x, name='Discovery', task='Inspect'))['result']
    url = f"/api/teams/{x['id']}/plan"
    response = await on.patch(url, json={'version': 2, 'requirements': [{'text': 'Import CSV'}, {'text': 'Export CSV'}]})
    assert response.status_code == 200
    a = SimpleNamespace(id='a' * 32, team_id=x['id'], state='reported', requirement_ids=['R-1'], need={})
    b = SimpleNamespace(id='b' * 32, team_id=x['id'], state='verified', requirement_ids=['R-2'], need={})
    attempt = SimpleNamespace(id=helper['id'], assignment_id=a.id, session_id=helper['id'], state='running')
    monkeypatch.setattr(controls, 'assignment_rows', lambda db, team: [a, b])
    monkeypatch.setattr(controls, 'attempt_rows', lambda db, identity: [attempt] if identity == a.id else [])
    changed = await on.patch(url, json={'version': 3, 'requirements': [{'id': 'R-1', 'text': 'Import CSV locally'}]})
    assert changed.status_code == 200, changed.text
    assert a.state == 'needs_revalidation' and b.state == 'verified'
    with on.app.state.supervisor.db() as db:
        notices = list(db.scalars(select(planning.PlanChangeNotice).where(planning.PlanChangeNotice.team_id == x['id'])))
        assert len(notices) == 1 and notices[0].plan_version == 4
        message = db.get(delivery.MessageDelivery, notices[0].delivery_id)
        assert message.session_id == helper['id'] and 'version 4' in message.body
        assert message.actor == 'supervisor'
        assert delivery.authorized(db, db.get(planning.Session, helper['id']), message.actor)
    changed = await on.patch(url, json={'version': 4, 'constraints': [{'text': 'No uploads'}]})
    assert changed.status_code == 200 and b.state == 'needs_revalidation'
    with on.app.state.supervisor.db() as db:
        notices = list(db.scalars(select(planning.PlanChangeNotice).where(planning.PlanChangeNotice.team_id == x['id'])))
        assert {n.plan_version for n in notices} == {4, 5}
        assert len({n.delivery_id for n in notices}) == 1  # queued notices coalesce, version records remain
        assert 'version 5' in db.get(delivery.MessageDelivery, notices[-1].delivery_id).preview


async def test_stale_proposal_cannot_replace_a_newer_person_boundary(on, project, env):
    x = await lead(on, project, env)
    url = f"/api/teams/{x['id']}/plan"
    assert (await on.patch(url, json={'version': 0, 'constraints': [{'text': 'No cloud'}]})).status_code == 200
    assert (await plan(on, env, x, propose_constraints=[{'id': 'C-1', 'text': 'Cloud allowed'}]))['ok']
    assert (await on.patch(url, json={'version': 2, 'constraints': [{'id': 'C-1', 'text': 'Offline only'}]})).status_code == 200
    card = next(i for i in (await on.get(f"/api/teams/{x['id']}/worklist")).json()['items'] if i['kind'] == 'decision')
    response = await on.patch(f"/api/teams/{x['id']}/worklist/items/{card['id']}",
        json={'revision': card['revision'], 'answer': 'accept'})
    assert response.status_code == 409
    assert 'boundary changed' in response.json()['detail']
    assert response.json()['item']['id'] == card['id']
    assert response.json()['item']['revision'] == card['revision']
    assert response.json()['item']['answer'] is None
    assert (await on.get(url)).json()['constraints'][0]['text'] == 'Offline only'
    cards = (await on.get(f"/api/teams/{x['id']}/worklist")).json()['items']
    assert next(i for i in cards if i['id'] == card['id'])['answer'] is None


async def test_dropped_phase_refuses_assignment_start_and_invalid_references(on, project, env):
    x = await lead(on, project, env)
    assert (await plan(on, env, x, idea='CSV', scope=SCOPE, phases=PHASES))['ok']
    assert (await plan(on, env, x, phases=[{'key': 'schema', 'status': 'dropped'}], reason='Drop schema'))['ok']
    with on.app.state.supervisor.db() as db:
        assignment = SimpleNamespace(team_id=x['id'], task_type='implementation', requirement_ids=[], need={'phase': 'importer'})
        with pytest.raises(LaunchRefused, match='blocked by schema'):
            planning.check_assignment(db, x['id'], assignment, LaunchInputs(provider='claude'))
        assignment.need = {}
        assignment.requirement_ids = ['R-999']
        with pytest.raises(LaunchRefused, match='R-999 is not active'):
            planning.check_assignment(db, x['id'], assignment, LaunchInputs(provider='claude'))
        assignment.team_id = 'another-team'
        with pytest.raises(LaunchRefused, match='another team'):
            planning.check_assignment(db, x['id'], assignment, LaunchInputs(provider='claude'))


def test_upgrade_from_main_planning_head_and_rollback_preserve_old_plan(tmp_path):
    from alembic import command
    from sqlalchemy import create_engine, inspect, text

    from acc.server.db import alembic_config

    url = 'sqlite:///' + str(tmp_path / 'migration.sqlite')
    config = alembic_config(url)
    command.upgrade(config, '0073')
    engine = create_engine(url)
    with engine.begin() as c:
        c.execute(text("INSERT INTO team (id,limits,expires_at,stopped,started) VALUES ('lead','{}','2030-01-01',0,0)"))
        c.execute(text("INSERT INTO orchestration_plan (team_id,state,idea,scope,version,person_edits,lead_seen_version,updated_at) "
                       "VALUES ('lead','ready','Keep me','{}',7,'{}',7,'2026-10-27')"))
        c.execute(text("INSERT INTO plan_phase (team_id,key,position,title,summary,\"check\",status,depends_on) "
                       "VALUES ('lead','one',1,'One','','','planned','[]')"))
    command.upgrade(config, 'head')
    with engine.connect() as c:
        assert c.execute(text('SELECT idea,version,run_mode,build_inputs FROM orchestration_plan')).one() == ('Keep me', 7, 'build', '{}')
        assert c.execute(text('SELECT requirement_ids FROM plan_phase')).scalar_one() == '[]'
        assert {'plan_constraint', 'plan_requirement', 'plan_change_notice', 'plan_proposal'} <= set(inspect(c).get_table_names())
    command.downgrade(config, '0073')
    with engine.connect() as c:
        assert c.execute(text('SELECT idea,version FROM orchestration_plan')).one() == ('Keep me', 7)
        assert 'plan_constraint' not in inspect(c).get_table_names()
    command.upgrade(config, 'head')
    engine.dispose()


async def test_entering_prepare_requires_confirmed_stops_of_all_helpers(on, project, env):
    x = await lead(on, project, env)
    assert (await plan(on, env, x, idea='CSV', scope=SCOPE, phases=PHASES[:1]))['ok']
    assert (await request(on, env, x, 'plan_ready', reason='Ready'))['ok']
    helper = (await assigned_start(on, env, x, name='Builder', task='Build'))['result']
    await on.app.state.supervisor.stop(x['id'], 'owner')
    url = f"/api/teams/{x['id']}/plan"
    response = await on.patch(url, json={'version': 2, 'run_mode': 'prepare'})
    assert response.status_code == 409 and 'stop the helpers' in response.text
    await on.app.state.supervisor.stop(helper['id'], 'owner')
    response = await on.patch(url, json={'version': 2, 'run_mode': 'prepare'})
    assert response.status_code == 200
    current = (await on.get(f"/api/sessions/{x['id']}")).json()
    assert current['launch_spec']['inputs']['permission_mode'] == 'plan'
    assert (await on.get(url)).json()['run_mode'] == 'prepare'


async def test_change_notice_is_authorized_for_real_fixture_delivery(on, project, env, monkeypatch):
    from helpers import wait_for

    from acc.server.orchestrator import planning_controls as controls

    x = await lead(on, project, env)
    assert (await plan(on, env, x, idea='CSV', scope=SCOPE, phases=PHASES[:1]))['ok']
    assert (await request(on, env, x, 'plan_ready', reason='Ready'))['ok']
    helper = (await assigned_start(on, env, x, name='Worker', task='Inspect'))['result']
    a = SimpleNamespace(id='a' * 32, team_id=x['id'], state='running', requirement_ids=[], need={})
    attempt = SimpleNamespace(id=helper['id'], assignment_id=a.id, session_id=helper['id'], state='running')
    monkeypatch.setattr(controls, 'assignment_rows', lambda db, team: [a])
    monkeypatch.setattr(controls, 'attempt_rows', lambda db, identity: [attempt])
    url = f"/api/teams/{x['id']}/plan"
    response = await on.patch(url, json={'version': 2, 'constraints': [{'text': 'Local files only'}]})
    assert response.status_code == 200
    sup = on.app.state.supervisor
    with sup.db() as db:
        notice = db.scalar(select(planning.PlanChangeNotice).where(planning.PlanChangeNotice.team_id == x['id']))
        identity = notice.delivery_id
    monkeypatch.setenv('ACC_ORCHESTRATOR_V2_DELIVERY', '1')
    async def acknowledged():
        await delivery.process_once(sup)
        with sup.db() as db:
            return db.get(delivery.MessageDelivery, identity).state == 'acknowledged'
    await wait_for(acknowledged)
    with sup.db() as db:
        message = db.get(delivery.MessageDelivery, identity)
        assert message.ack == 'hook:UserPromptSubmit' and message.actor == 'supervisor'
        assert 'Plan change notice version 3' in message.preview


async def test_assignment_start_uses_card_check_through_real_team_spool(on, project, env, monkeypatch):
    x = await lead(on, project, env)
    url = f"/api/teams/{x['id']}/plan"
    assert (await on.patch(url, json={'version': 0, 'requirements': [{'text': 'Import CSV'}],
        'idea': 'CSV', 'scope': SCOPE, 'phases': [{'key': 'import', 'title': 'Import', 'requirement_ids': ['R-1']}]})).status_code == 200
    assert (await request(on, env, x, 'plan_ready', reason='Ready'))['ok']
    assignment, selected = await real_assignment(on, env, x, ['R-1'], monkeypatch)
    card = await request(on, env, x, 'worklist_add', kind='decision', title='Person must approve import',
        options=[{'id': 'yes', 'label': 'Approve'}, {'id': 'no', 'label': 'Decline'}], blocks=['R-1'])
    assert card['ok']
    refused = await request(on, env, x, 'start', name='Blocked', assignment=assignment, selection=selected)
    assert not refused['ok'] and 'Person must approve import' in refused['error']
    item = card['result']
    assert (await on.patch(f"/api/teams/{x['id']}/worklist/items/{item['id']}",
        json={'revision': item['revision'], 'answer': 'yes'})).status_code == 200
    started = await request(on, env, x, 'start', name='Allowed', assignment=assignment, selection=selected)
    assert started['ok'], started


def test_dropped_ancestor_blocks_even_a_previously_done_intermediate_phase():
    rows = {p.key: p for p in [
        SimpleNamespace(key='a', status='dropped', depends_on=[]),
        SimpleNamespace(key='b', status='done', depends_on=['a']),
        SimpleNamespace(key='c', status='planned', depends_on=['b'])]}
    assert planning._blocked_by(rows['c'], rows) == ['b']


def test_dependency_walk_memoizes_shared_done_ancestors():
    reads = []
    class Phase:
        def __init__(self, key, dependencies):
            self.key, self.depends_on = key, dependencies
        @property
        def status(self):
            reads.append(self.key)
            return 'done'
    rows = {}
    for i in range(12):
        key = str(i)
        rows[key] = Phase(key, list(rows)[-10:])
    assert planning._blocked_by(rows['11'], rows) == []
    assert len(reads) <= len(rows)  # shared ancestors are not re-walked exponentially


async def test_requirement_change_revalidates_dependent_phases_not_unrelated_work(on, project, env, monkeypatch):
    from acc.server.orchestrator import planning_controls as controls
    x = await lead(on, project, env)
    url = f"/api/teams/{x['id']}/plan"
    response = await on.patch(url, json={'version': 0,
        'requirements': [{'text': 'Schema'}, {'text': 'Importer'}, {'text': 'Documentation'}],
        'phases': [{'key': 'schema', 'title': 'Schema', 'requirement_ids': ['R-1']},
                   {'key': 'import', 'title': 'Import', 'requirement_ids': ['R-2'], 'depends_on': ['schema']},
                   {'key': 'docs', 'title': 'Docs', 'requirement_ids': ['R-3']}]})
    assert response.status_code == 200
    dependent = SimpleNamespace(id='a' * 32, team_id=x['id'], state='verified', requirement_ids=['R-2'], need={})
    unrelated = SimpleNamespace(id='b' * 32, team_id=x['id'], state='reported', requirement_ids=['R-3'], need={})
    monkeypatch.setattr(controls, 'assignment_rows', lambda db, team: [dependent, unrelated])
    monkeypatch.setattr(controls, 'attempt_rows', lambda db, assignment: [])
    changed = await on.patch(url, json={'version': 1, 'requirements': [{'id': 'R-1', 'text': 'Revised schema'}]})
    assert changed.status_code == 200
    assert dependent.state == 'needs_revalidation' and unrelated.state == 'reported'


async def test_existing_brief_constraint_changes_only_after_person_accepts_new_version(on, project, env):
    x = await lead(on, project, env)
    with on.app.state.supervisor.db() as db:
        planning.draft_brief(db, x['id'], 'casey', 1, [], ['Local files only'])
        draft = planning.draft_brief(db, x['id'], f"session:{x['id']}", 2, [],
            [{'id': 'C-1', 'text': 'Allow an approved remote machine'}])
        assert draft['constraints'][0]['text'] == 'Local files only'
        assert draft['constraints'][0]['brief_version'] == 1
        assert planning.version(db, x['id'], 1)['plan']['constraints'][0]['text'] == 'Local files only'
        db.commit()
    card = next(i for i in (await on.get(f"/api/teams/{x['id']}/worklist")).json()['items'] if i['kind'] == 'decision')
    response = await on.patch(f"/api/teams/{x['id']}/worklist/items/{card['id']}",
        json={'revision': card['revision'], 'answer': 'accept'})
    assert response.status_code == 200, response.text
    row = (await on.get(f"/api/teams/{x['id']}/plan")).json()['constraints'][0]
    assert row['brief_version'] == 2 and row['source'] == 'brief'
    assert row['text'] == 'Allow an approved remote machine'


async def test_migrated_plan_keeps_original_permissions_across_prepare_and_build(on, project, env):
    x = await lead(on, project, env)
    sup = on.app.state.supervisor
    with sup.db() as db:
        planning.plan_for(db, x['id']).build_inputs = {}  # migration's default for an existing plan
        db.commit()
    await sup.stop(x['id'], 'owner')
    url = f"/api/teams/{x['id']}/plan"
    assert (await on.patch(url, json={'version': 0, 'run_mode': 'prepare'})).status_code == 200
    assert (await on.get(f"/api/sessions/{x['id']}")).json()['launch_spec']['inputs']['permission_mode'] == 'plan'
    assert (await on.patch(url, json={'version': 1, 'run_mode': 'build'})).status_code == 200
    current = (await on.get(f"/api/sessions/{x['id']}")).json()
    assert current['launch_spec']['inputs']['permission_mode'] == 'acceptEdits'
    with sup.db() as db:
        from acc.server.models import Team
        assert db.get(Team, x['id']).limits['agents'][0]['inputs']['permission_mode'] == 'acceptEdits'


async def test_card_added_while_worktree_is_prepared_refuses_helper_publication(on, project, env, monkeypatch):
    x = await lead(on, project, env)
    assert (await on.patch(f"/api/teams/{x['id']}/plan", json={'version': 0, 'requirements': [{'text': 'Import'}],
        'idea': 'CSV', 'scope': SCOPE, 'phases': [{'key': 'import', 'title': 'Import', 'requirement_ids': ['R-1']}]})).status_code == 200
    assert (await request(on, env, x, 'plan_ready', reason='Ready'))['ok']
    assignment, selected = await real_assignment(on, env, x, ['R-1'], monkeypatch)
    sup = on.app.state.supervisor
    original = sup.broker.call
    async def call(op, **kwargs):
        result = await original(op, **kwargs)
        if op == 'make_worktree':
            with sup.db() as db:
                worklist.add(db, x['id'], 'owner', worklist.person_source('owner'), {
                    'kind': 'decision', 'title': 'New material decision', 'blocks': ['R-1'],
                    'options': [{'id': 'yes', 'label': 'Approve'}, {'id': 'no', 'label': 'Decline'}]})
                db.commit()
        return result
    monkeypatch.setattr(sup.broker, 'call', call)
    result = await request(on, env, x, 'start', name='Wait', assignment=assignment, selection=selected)
    assert not result['ok'] and 'New material decision' in result['error']
    with sup.db() as db:
        assert list(db.scalars(select(planning.Session).where(planning.Session.team_id == x['id']))) == [db.get(planning.Session, x['id'])]


async def test_prepare_refuses_server_side_code_writes_even_when_preview_is_disabled(on, project, env, monkeypatch):
    x = await lead(on, project, env, run_mode='prepare')
    refused = await request(on, env, x, 'merge', helper='unknown')
    assert not refused['ok'] and 'prepare mode' in refused['error']
    monkeypatch.setenv('ACC_ORCHESTRATOR', '0')
    refused = await request(on, env, x, 'merge', helper='unknown')
    assert not refused['ok'] and 'prepare mode' in refused['error']


async def test_prepare_cannot_resume_a_previously_granted_write_helper(on, project, env):
    x = await lead(on, project, env)
    assert (await plan(on, env, x, idea='CSV', scope=SCOPE, phases=PHASES[:1]))['ok']
    assert (await request(on, env, x, 'plan_ready', reason='Ready'))['ok']
    helper = (await assigned_start(on, env, x, name='Builder', task='Build'))['result']
    sup = on.app.state.supervisor
    await sup.stop(helper['id'], 'owner')
    await sup.stop(x['id'], 'owner')
    assert (await on.patch(f"/api/teams/{x['id']}/plan", json={'version': 2, 'run_mode': 'prepare'})).status_code == 200
    response = await on.post(f"/api/sessions/{helper['id']}/resume")
    assert response.status_code == 409 and 'read-only discovery' in response.text


async def real_assignment(api, env, caller, requirements, monkeypatch):
    from acc.server.orchestrator import selection
    async def ready(supervisor, account, machines, agents):
        return {(machine, agent): {'logged_in': True, 'models': [{'id': 'model-a'}]}
                for machine in machines for agent in agents}
    assigned = await request(api, env, caller, 'assign', goal='Import CSV', task_type='implementation',
        acceptance=[], requirement_ids=requirements, need={'role': 'general', 'classification_reason': 'Bounded import task'})
    assert assigned['ok'], assigned
    identity = assigned['result']['id']
    monkeypatch.setattr(selection, 'readiness', ready)
    picked = await request(api, env, caller, 'select', assignment=identity)
    assert picked['ok'] and picked['result']['chosen'] is not None, picked
    return identity, picked['result']['id']


async def test_prepare_helpers_need_a_real_read_only_launch_not_only_plan_mode(on, project, env):
    x = await lead(on, project, env, run_mode='prepare')
    with on.app.state.supervisor.db() as db:
        assignment = SimpleNamespace(team_id=x['id'], task_type='discovery', requirement_ids=[], need={})
        for inputs in (LaunchInputs(provider='copilot', options={'mode': 'plan', 'permissions': 'all'}),
                       LaunchInputs(provider='copilot', options={'mode': 'plan', 'permissions': 'tools'}),
                       LaunchInputs(provider='claude', permission_mode='plan', extra_args=('--allowedTools', 'Bash')),
                       LaunchInputs(provider='cursor', options={'mode': 'plan', 'force': True}),
                       LaunchInputs(provider='antigravity', options={'mode': 'plan', 'skip_permissions': True})):
            with pytest.raises(LaunchRefused, match='read-only discovery'):
                planning.check_assignment(db, x['id'], assignment, inputs)
        planning.check_assignment(db, x['id'], assignment, LaunchInputs(provider='copilot', options={'mode': 'plan', 'permissions': 'ask'}))
        planning.check_assignment(db, x['id'], assignment, LaunchInputs(provider='codex', sandbox='read-only'))


async def test_a_card_blocking_a_requirement_blocks_its_phase_and_unnamed_starts(on, project, env):
    x = await lead(on, project, env)
    assert (await on.patch(f"/api/teams/{x['id']}/plan", json={'version': 0, 'requirements': [
        {'text': 'Import CSV'}, {'text': 'Export CSV'}]})).status_code == 200
    assert (await plan(on, env, x, idea='CSV', scope=SCOPE, phases=[
        {'key': 'import', 'title': 'Import', 'requirement_ids': ['R-1']},
        {'key': 'export', 'title': 'Export', 'requirement_ids': ['R-2']}]))['ok']
    asked = await request(on, env, x, 'worklist_add', kind='decision', title='Approve import privacy',
        options=[{'id': 'yes', 'label': 'Approve'}, {'id': 'no', 'label': 'Decline'}], blocks=['R-1'])
    assert asked['ok'], asked
    with on.app.state.supervisor.db() as db:
        by_phase = SimpleNamespace(team_id=x['id'], task_type='implementation', requirement_ids=[], need={'phase': 'import'})
        with pytest.raises(LaunchRefused, match='Approve import privacy'):
            planning.check_assignment(db, x['id'], by_phase, LaunchInputs(provider='claude'))
        unnamed = SimpleNamespace(team_id=x['id'], task_type='implementation', requirement_ids=[], need={})
        with pytest.raises(LaunchRefused, match='requirement_ids or phase'):
            planning.check_assignment(db, x['id'], unnamed, LaunchInputs(provider='claude'))
        planning.check_assignment(db, x['id'], SimpleNamespace(team_id=x['id'], task_type='implementation',
                                  requirement_ids=['R-2'], need={}), LaunchInputs(provider='claude'))


async def test_lead_proposals_are_labelled_the_leads_and_have_their_own_budget(on, project, env, monkeypatch):
    from acc.server.orchestrator import planning_controls
    x = await lead(on, project, env)
    url = f"/api/teams/{x['id']}/plan"
    assert (await request(on, env, x, 'plan'))['ok']
    proposed = await plan(on, env, x, propose_constraints=[{'text': 'Use the cloud cache'}])
    assert proposed['ok'], proposed
    row = (await on.get(url)).json()['constraints'][0]
    assert row['state'] == 'proposed_change' and row['source'] == 'lead'
    with on.app.state.supervisor.db() as db:
        for n in range(19):
            db.add(planning_controls.Constraint(team_id=x['id'], id=f'C-{n + 2}', text=f'Proposal {n}', source='lead',
                                                version=1, state='proposed_change'))
        db.commit()
    refused = await plan(on, env, x, propose_constraints=[{'text': 'One more'}])
    assert not refused['ok'] and 'at most 20 proposed rows' in refused['error']
    # The person can still add their own boundaries.
    person = await on.patch(url, json={'version': (await on.get(url)).json()['version'], 'constraints': [{'text': 'Offline only'}]})
    assert person.status_code == 200, person.text
    assert any(r['text'] == 'Offline only' and r['source'] == 'person' and r['state'] == 'active'
               for r in person.json()['constraints'])


@pytest.mark.parametrize('actor', ['system', 'supervisor', 'nobody-signed-up', 'session:' + 'a' * 32, 'schedule:' + 'b' * 32, None])
async def test_only_a_signed_in_person_counts_as_the_person(on, project, env, actor):
    from acc.server.orchestrator import planning_controls
    x = await lead(on, project, env)
    with on.app.state.supervisor.db() as db:
        with pytest.raises(worklist.Refused, match='only the person'):
            planning_controls.edit_rows(db, x['id'], actor, [{'text': 'Injected'}], planning_controls.Constraint)
        with pytest.raises(worklist.Refused, match='only the person'):
            planning_controls.set_run_mode(db, x['id'], actor, 'build')
        if isinstance(actor, str):
            planning_controls.draft_brief(db, x['id'], actor, 1, [{'text': 'R', 'acceptance': 'A'}], [])
            rows = planning_controls.rows(db, x['id'], planning_controls.Requirement)
            assert [r.state for r in rows] == ['proposed_change']  # never accepted as the person's
        db.rollback()



@pytest.mark.parametrize('actor', ['casey', '@api'])
async def test_a_signed_in_person_or_the_owner_token_is_the_person(on, project, env, actor):
    from acc.server.orchestrator import planning_controls
    x = await lead(on, project, env)
    with on.app.state.supervisor.db() as db:
        planning_controls.edit_rows(db, x['id'], actor, [{'text': 'Offline only'}], planning_controls.Constraint)
        row = planning_controls.rows(db, x['id'], planning_controls.Constraint)[0]
        assert row.state == 'active' and row.source == 'person'
        db.rollback()
