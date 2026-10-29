"""Owner acceptance contracts for orch A–G, against the approved 2026-10-27 spec.

Merged A/B/C/D/E/J scenarios are active; fixture/runtime errors remain visible.
No live providers or agent executables are invoked: api/env own the fake CLIs,
broker, tmux socket, repositories and accounts. Model inventory is a test input;
selection, grants, reports, Git, process cancellation and persistence stay real.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import subprocess
import sys
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from helpers import pause_supervisor, wait_for, write_db
from sqlalchemy import select
from test_auth import do_setup
from test_changes import git, repo
from test_team import grant
from test_team import request as team_request

from acc.server.models import Session, Team, utcnow
from acc.server.orchestrator import gates, planning, worklist
from acc.server.supervisor_common import LaunchRefused


@pytest.fixture(autouse=True)
def orchestrator_switches(monkeypatch):
    # Exercise the target features explicitly; default-on is lane F's separate scope.
    for flag in ('ACC_ORCHESTRATOR', 'ACC_ORCHESTRATOR_V2', 'ACC_ORCHESTRATOR_V2_DELIVERY'):
        monkeypatch.setenv(flag, '1')
    monkeypatch.setattr(gates, 'TICK', .05)


@pytest.fixture
async def model_inventory(api, monkeypatch):
    """Controlled broker observations, not a replacement selector or ranking policy."""
    original = api.app.state.supervisor.broker.call
    async def call(method, **params):
        if method == 'auth_health':
            return {'checked_at': utcnow().timestamp(),
                    **{agent: {'logged_in': True} for agent in ('claude', 'codex', 'copilot')}}
        if method == 'model_catalog':
            return {'fetched_at': utcnow().timestamp(), 'errors': {},
                    **{agent: [{'id': 'text-model', 'price_class': 'cheap', 'image_input': False},
                            {'id': 'image-model', 'price_class': 'cheap', 'image_input': True},
                            {'id': 'unknown-image-model', 'price_class': 'cheap'}]
                    for agent in ('claude', 'codex', 'copilot')}}
        return await original(method, **params)
    monkeypatch.setattr(api.app.state.supervisor.broker, 'call', call)


def result(reply):
    assert reply.get('ok') is True, reply
    assert isinstance(reply.get('result'), dict), reply
    return reply['result']


async def tool(api, env, caller, action, **payload):
    return await team_request(api, env, caller, action, **payload)


async def ready_lead(api, env, project, *, agents=None, **limits):
    """Existing, stable launch/plan protocol. Fixture regressions must not xfail."""
    root = Path(project['workspace_path'])
    if not (root / '.git').exists():
        repo(root)
    allowed = agents or [{'agent': 'claude', 'profile': None, 'permission_mode': 'acceptEdits'}]
    response = await api.post('/api/sessions', json={
        'project_id': project['id'], 'provider': 'claude', 'name': 'Acceptance lead', 'worktree': True,
        'orchestrator': grant(agents=allowed, **limits),
    })
    if response.status_code != 201:
        raise RuntimeError(f'Acceptance fixture launch failed: {response.status_code} {response.text}')
    lead = response.json()
    for action, payload in [
        ('plan_update', {'reason': 'Acceptance fixture scope', 'idea': 'Build a small CSV importer.',
                         'scope': {'in': ['CSV import'], 'out': ['Cloud synchronization'], 'parked': []},
                         'phases': [{'key': 'implementation', 'title': 'Implement and verify CSV import'}]}),
        ('plan_ready', {'reason': 'The fixture scope is ready'}),
    ]:
        reply = await tool(api, env, lead, action, **payload)
        if not reply.get('ok'):
            raise RuntimeError(f'Acceptance fixture {action} failed: {reply}')
    return lead


async def assign(api, env, lead, goal, *, task_type='implementation', role='general', requirement_ids=None, **need):
    if task_type in ('review', 'security_review'):
        need = {'author_session': lead['id'], 'effort': 'plan', **need}
    return result(await tool(api, env, lead, 'assign', goal=goal, task_type=task_type,
                             acceptance=['Deliver the described result and related evidence.'],
                             requirement_ids=requirement_ids or [], need={'role': role, 'task_type': task_type, 'classification_reason': 'Acceptance fixture task and smallest sufficient role', **need}))


async def choose(api, env, lead, assignment, **payload):
    return result(await tool(api, env, lead, 'select', assignment=assignment['id'], **payload))


async def start(api, env, lead, assignment, selection, name, **payload):
    reply = await tool(api, env, lead, 'start', assignment=assignment['id'], selection=selection['id'], name=name, **payload)
    result(reply)
    # The spec fixes Attempt's fields, but not start's return envelope. Observe
    # the durable attempt rather than guessing whether its response is nested.
    from acc.server.orchestrator.assignments import Attempt
    with api.app.state.db() as db:
        attempts = list(db.scalars(select(Attempt).where(Attempt.assignment_id == assignment['id'])
                                  .order_by(Attempt.created_at, Attempt.id)))
        assert attempts and attempts[-1].session_id
        attempt_id, session_id = attempts[-1].id, attempts[-1].session_id
    response = await api.get(f'/api/sessions/{session_id}')
    assert response.status_code == 200
    return response.json(), attempt_id


async def selected_helper(api, env, lead, goal, name, *, role='general', **payload):
    assignment = await assign(api, env, lead, goal, role=role)
    selection = await choose(api, env, lead, assignment)
    helper, attempt = await start(api, env, lead, assignment, selection, name, **payload)
    return helper, assignment, attempt


async def launch_text(session, env):
    # A intentionally overlays roles at start/resume, not in the frozen LaunchSpec.
    run = Path(env['run_dir']) / session['id']
    names = list(session['launch_spec']['files'])
    async def written():
        return all((run / name).is_file() for name in names)
    await wait_for(written)
    return ('\n'.join((run / name).read_text() for name in names) + '\n'
            + (session['launch_spec']['inputs'].get('initial_prompt') or '')).lower()


def git_head(path, ref='HEAD'):
    return subprocess.check_output(['git', '-C', str(path), 'rev-parse', ref], text=True).strip()


@pytest.fixture
async def gate_world(api, project, monkeypatch):
    """Owner-set tiny commands, actual isolated broker processes, cleanup even on xfail."""
    root = Path(project['workspace_path'])
    repo(root)
    command = "import sys; open(sys.argv[1], 'w').write('<testsuite><testcase classname=\"tests.test_demo\" name=\"test_one\"/></testsuite>')"
    body = {'full': [sys.executable, '-c', command, '{junit}'],
            'related': [sys.executable, '-c', command, '{junit}'],
            'rerun': [sys.executable, '-c', command, '{junit}', '{tests}']}
    response = await api.put(f"/api/gates/projects/{project['id']}/commands", json=body)
    if response.status_code != 200:
        raise RuntimeError(response.text)
    response = await api.put('/api/gates/budget', json={'enabled': True, 'confirm_branch_code': True})
    if response.status_code != 200:
        raise RuntimeError(response.text)
    try:
        yield root, body
    finally:
        # A missing Stop-team implementation must not leave the test's child alive.
        for row in (await api.get('/api/gates')).json()['jobs']:
            if row['state'] in ('queued', 'running'):
                await api.post(f"/api/gates/{row['id']}/cancel")
        async def ended():
            return all(j['state'] not in ('queued', 'running') for j in (await api.get('/api/gates')).json()['jobs'])
        await wait_for(ended, timeout=15)


async def gate_row(api, identifier):
    rows = (await api.get('/api/gates')).json()['jobs']
    return next(row for row in rows if row['id'] == identifier)


async def test_verification_freshness(api, project, gate_world):
    """Scenario 9: base/commands stale a pass; a fresh retry pass is not integration evidence."""
    root, commands = gate_world
    git(root, 'branch', 'acc/acceptance-clean')
    queued = await api.post('/api/gates', json={'project': project['id'], 'branch': 'acc/acceptance-clean'})
    assert queued.status_code == 201
    job_id = queued.json()['id']
    async def passed():
        row = await gate_row(api, job_id)
        return row if row['state'] == 'passed' else None
    original = await wait_for(passed)
    assert original.get('base_sha') == git_head(root, 'main')
    assert original.get('commands_digest') and original.get('runner_version')
    (root / 'base-change.txt').write_text('The owner advanced the base.\n')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'Advance the acceptance base')
    assert (await gate_row(api, job_id))['stale'] == 'base moved'
    # Commands take precedence when both command and base evidence have moved.
    changed = {**commands, 'related': commands['related'] + ['changed-command']}
    assert (await api.put(f"/api/gates/projects/{project['id']}/commands", json=changed)).status_code == 200
    assert (await gate_row(api, job_id))['stale'] == 'commands changed'
    failing = "import sys; open(sys.argv[1], 'w').write('<testsuite><testcase classname=\"tests.test_demo\" name=\"test_one\"><failure message=\"original failure\"/></testcase></testsuite>'); sys.exit(1)"
    # JUnit rerun IDs are accepted only for files actually in the tested checkout.
    (root / 'tests').mkdir(exist_ok=True)
    (root / 'tests/test_demo.py').write_text('def test_one(): pass\n')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'Add the tiny retry fixture')
    git(root, 'branch', 'acc/acceptance-retry')
    retry_commands = {**commands, 'related': [sys.executable, '-c', failing, '{junit}']}
    assert (await api.put(f"/api/gates/projects/{project['id']}/commands", json=retry_commands)).status_code == 200
    queued = await api.post('/api/gates', json={'project': project['id'], 'branch': 'acc/acceptance-retry'})
    assert queued.status_code == 201
    retry_id = queued.json()['id']
    async def terminal():
        row = await gate_row(api, retry_id)
        return row if row['state'] not in ('queued', 'running') else None
    retried = await wait_for(terminal)
    assert retried['state'] == 'retry-passed'
    assert retried['results']['exit'] == 1 and retried['results']['rerun']['exit'] == 0
    assert not retried['results'].get('accepted_flake')


async def test_stop_and_recovery(api, env, project, gate_world):
    """Scenario 11: actual gate cancellation, honest offline stop, bounded expiry lead."""
    root, commands = gate_world
    lead = await ready_lead(api, env, project)
    offline_id = uuid.uuid4().hex
    with write_db(api.app.state.db) as db:
        parent = db.get(Session, lead['id'])
        db.add(Session(id=offline_id, project_id=parent.project_id, provider=parent.provider, name='Offline helper',
                       tmux_name='fixture-offline-' + offline_id, launch_spec_id=parent.launch_spec_id,
                       run_as=parent.run_as, owner=parent.owner, team_id=parent.team_id,
                       parent_session_id=parent.id, team_depth=1, helper_kind='temporary', team_host='away-node',
                       desired_state='running', observed_state='active'))
        db.commit()
    sleeping = {**commands, 'related': [sys.executable, '-c', 'import time; time.sleep(60)', '{junit}']}
    assert (await api.put(f"/api/gates/projects/{project['id']}/commands", json=sleeping)).status_code == 200
    job = result(await tool(api, env, lead, 'gate', suite='related'))
    async def running():
        return (await gate_row(api, job['id']))['state'] == 'running'
    await wait_for(running)
    stopped = await api.post(f"/api/sessions/{lead['id']}/team/stop")
    assert stopped.status_code == 200
    async def cancelled():
        row = await gate_row(api, job['id'])
        return row if row['state'] == 'cancelled' else None
    row = await wait_for(cancelled, timeout=5)
    assert row['reason'] == 'cancelled: team stopped'
    await do_setup(api, env)
    response = await api.get(f"/api/teams/{lead['id']}/tree", headers={'Authorization': ''})
    assert response.status_code == 200, response.text
    tree = response.json()['members']
    offline = next(member for member in tree if member['id'] == offline_id)
    assert offline.get('stop_state') == 'stop requested'
    assert offline.get('stop_state') != 'stop confirmed'
    second = await ready_lead(api, env, project)
    sup = api.app.state.supervisor
    await pause_supervisor(api)
    with write_db(sup.db) as db:
        db.get(Team, second['id']).expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    await sup.expire_teams()
    snapshot = (await api.get(f"/api/sessions/{second['id']}/team")).json()
    assert snapshot.get('wrapup_until')
    with write_db(sup.db) as db:
        team = db.get(Team, second['id'])
        assert team.wrapup_until == team.expires_at + timedelta(minutes=30)
        assert db.get(Session, second['id']).desired_state == 'running'
        team.wrapup_until = utcnow() - timedelta(seconds=1)
        db.commit()
    await sup.expire_teams()
    assert (await api.get(f"/api/sessions/{second['id']}")).json()['desired_state'] == 'stopped'
    final = (await api.get(f"/api/sessions/{second['id']}/team")).json()
    assert next(m for m in final['members'] if m['id'] == second['id'])['stop_state'] == 'stop confirmed'
    # A persisted completed/archived member must be distinguished from a pending stop.
    with write_db(sup.db) as db:
        member = db.get(Session, offline_id)
        member.team_stop_pending = False
        member.desired_state = 'archived'
        db.commit()
    final = (await api.get(f"/api/sessions/{lead['id']}/team")).json()
    assert next(m for m in final['members'] if m['id'] == offline_id)['stop_state'] == 'finished'


@pytest.mark.parametrize('provider', ['claude', 'copilot'])
async def test_effective_baseline(api, project, provider, env):
    """Scenario 1: inspect the effective no-profile launch, not catalog source."""
    response = await api.post('/api/sessions', json={
        'project_id': project['id'], 'provider': provider, 'name': 'No-profile baseline'})
    assert response.status_code == 201, response.text
    text = await launch_text(response.json(), env)
    # Concepts intentionally tolerate wording polish permitted by A1.
    for alternatives in [('evidence, not instructions', 'evidence rather than instructions'),
                         ('never work around a refusal',), ('material ambiguity',),
                         ('secrets',), ('handoff',), ('verification',), ('report',)]:
        assert any(phrase in text for phrase in alternatives), alternatives
    for word in ('review', 'grant', 'workspace', 'permissions', 'branch', 'reversible',
                 'commands', 'revision', 'skipped', 'stops', 'docs', 'report tool'):
        assert word in text, word


async def test_task_specific_selection(api, env, project, model_inventory):
    """Scenario 2: manual pins obey task/profile and image requirements; rank is stable."""
    lead = await ready_lead(api, env, project, agents=[
        {'agent': 'claude', 'profile': 'security'}, {'agent': 'codex', 'profile': 'security'}])
    await do_setup(api, env)
    headers = {'Authorization': '', 'Origin': 'http://acc', 'X-ACC-CSRF': api.cookies.get('acc_csrf')}
    response = await api.get('/api/settings/orchestrator/selection', headers=headers)
    if response.status_code != 200:
        raise RuntimeError(f'Owner selection fixture failed: {response.text}')
    policy = response.json()
    policy['rules'] = [rule for rule in policy['rules'] if rule['task_type'] != 'docs'] + [
        {'profile': '*', 'task_type': 'docs', 'choices': [{'agent': 'codex', 'models': ['*']}]}]
    assert (await api.put('/api/settings/orchestrator/selection', json=policy, headers=headers)).status_code == 200
    review = await assign(api, env, lead, 'Review CSV parser security', task_type='security_review', role='security')
    denied = await tool(api, env, lead, 'select', assignment=review['id'], pin={'agent': 'codex', 'model': 'text-model'})
    assert denied['ok'] and denied['result']['chosen'] is None, denied
    one = await choose(api, env, lead, review)
    two = await choose(api, env, lead, review)
    assert one['chosen'] == two['chosen']
    assert one['candidates'][one['chosen']]['profile'] == 'security'


async def test_image_input_qualification(api, env, project, model_inventory, monkeypatch, tmp_path):
    from test_image_capabilities import fake_image_executable
    # Qualify through the real broker/probe and a fake CLI that decodes the generated PNG.
    # Raw model inventory vision flags alone must never qualify a model.
    monkeypatch.setitem(env['broker'].providers, 'claude', str(fake_image_executable(tmp_path)))
    broker = api.app.state.supervisor.broker
    original = broker.call
    async def observed(method, **params):
        reply = await original(method, **params)
        if method == 'model_catalog':
            identity = await original('image_identity', account=broker.default_account, host='local', provider='claude')
            reply['claude'] = [{**model, **identity} for model in reply['claude']]
        return reply
    monkeypatch.setattr(broker, 'call', observed)
    lead = await ready_lead(api, env, project, agents=[{'agent': 'claude', 'profile': 'security'}])
    visual = await assign(api, env, lead, 'Inspect the supplied image', role='security', task_type='design', effort='plan', requires=['image_input'])
    unqualified = await tool(api, env, lead, 'select', assignment=visual['id'])
    assert unqualified['ok'] and unqualified['result']['chosen'] is None, unqualified
    qualified = await api.post('/api/providers/image-check', json={
        'project_id': project['id'], 'provider': 'claude', 'model': 'image-model'})
    assert qualified.status_code == 200 and qualified.json()['image_input'] is True, qualified.text
    for model in ('text-model', 'unknown-image-model'):
        denied = await tool(api, env, lead, 'select', assignment=visual['id'], pin={'agent': 'claude', 'model': model})
        assert denied['ok'] and denied['result']['chosen'] is None, denied
    one = await choose(api, env, lead, visual)
    two = await choose(api, env, lead, visual)
    assert one['chosen'] == two['chosen']
    chosen = one['candidates'][one['chosen']]
    assert chosen['model'] == 'image-model'
    assert chosen['profile'] == 'security'


async def test_role_consistency(api, env, project, model_inventory):
    """Scenario 3: compiled depth-two context retains worker Git/authority boundaries."""
    lead = await ready_lead(api, env, project)
    first, _, _ = await selected_helper(api, env, lead, 'Implement parser', 'First')
    second, _, _ = await selected_helper(api, env, first, 'Test parser', 'Second')
    assert second['team_depth'] == 2
    for member in (lead, first, second):
        text = await launch_text(member, env)
        assert 'main' in text and ('person' in text or 'owner' in text)
        assert 'merge' in text and ('never' in text or 'must not' in text)
        assert 'evidence' in text and 'authority' in text
    assert 'integrat' in await launch_text(lead, env)
    assert 'assigned' in await launch_text(second, env)


async def test_reviewer_persistence(api, env, project, model_inventory):
    """Scenario 4: read-only findings survive; remediation and re-review are distinct attempts."""
    lead = await ready_lead(api, env, project, agents=[
        {'agent': 'claude', 'profile': 'security'}, {'agent': 'claude', 'profile': None}])
    assignment = await assign(api, env, lead, 'Review parser security', task_type='security_review', role='security')
    reviewer, attempt = await start(api, env, lead, assignment, await choose(api, env, lead, assignment), 'Reviewer')
    assert reviewer['launch_spec']['inputs']['permission_mode'] == 'plan'
    head = git_head(reviewer['worktree'])
    report = result(await tool(api, env, reviewer, 'report', attempt=attempt, status='done',
                               summary='Parser accepts unsafe traversal', findings_md='Reject ../ paths.',
                               reviewed_branch=lead['worktree_branch'], reviewed_head=head))
    assert report['findings_md'] == 'Reject ../ paths.'
    fixer, _, fix_attempt = await selected_helper(api, env, lead, 'Reject traversal and add a regression check', 'Fixer')
    assert fix_attempt != attempt and fixer['id'] != reviewer['id']
    path = Path(fixer['worktree'])
    (path / 'parser-fix.txt').write_text('Reject traversal before reading.\n')
    git(path, 'add', '.')
    git(path, 'commit', '-qm', 'Reject unsafe paths')
    fixed_head = git_head(path)
    rerun = await assign(api, env, lead, 'Review the fixed parser head', task_type='security_review', role='security', author_session=fixer['id'])
    second, second_attempt = await start(api, env, lead, rerun, await choose(api, env, lead, rerun), 'Re-review')
    evidence = result(await tool(api, env, second, 'report', attempt=second_attempt, status='done',
                                 summary='Reviewed fixed parser', findings_md='No remaining traversal finding.',
                                 reviewed_branch=fixer['worktree_branch'], reviewed_head=fixed_head))
    assert evidence['reviewed_head'] == fixed_head != head
    summary = (await api.get(f"/api/sessions/{lead['id']}/team")).json()
    fixed = next(m for m in summary['members'] if m['id'] == fixer['id'])
    assert fixed['verification']['review']['status'] == 'reviewed at this revision'
    (path / 'parser-fix.txt').write_text('Another change after review.\n')
    git(path, 'add', '.')
    git(path, 'commit', '-qm', 'Move beyond reviewed revision')
    summary = (await api.get(f"/api/sessions/{lead['id']}/team")).json()
    moved = next(m for m in summary['members'] if m['id'] == fixer['id'])
    assert moved['verification']['review']['status'] == 'not reviewed at this revision'
    from acc.server.orchestrator.delivery import TaskReport
    with api.app.state.db() as db:
        rows = list(db.scalars(select(TaskReport).where(TaskReport.attempt_id.in_([attempt, second_attempt]))))
        assert {row.attempt_id for row in rows} == {attempt, second_attempt}
        assert any(row.findings_md == 'Reject ../ paths.' for row in rows)


async def test_bounded_verification(api, env, project, model_inventory):
    """Scenario 5: effective team instructions route full suites to gates, or disclose limits.

    This checks the contract delivered to the provider, not an invented LLM's obedience.
    """
    lead = await ready_lead(api, env, project, agents=[{'agent': 'claude', 'profile': 'fullstack'}])
    helper, _, attempt = await selected_helper(api, env, lead, 'Implement parser using TDD', 'TDD worker', role='fullstack')
    text = await launch_text(helper, env)
    assert 'related' in text and 'full suite' in text
    assert 'gate' in text and ('must not' in text or 'do not' in text or 'never' in text)
    assert (await api.put('/api/gates/budget', json={'enabled': False})).status_code == 200
    limited, _, limited_attempt = await selected_helper(api, env, lead, 'Verify parser without a gate', 'Limited worker', role='fullstack')
    text = await launch_text(limited, env)
    assert 'verification is limited' in text or 'full suite not run' in text
    stored = result(await tool(api, env, limited, 'report', attempt=limited_attempt, status='done',
                               summary='Related checks passed; full suite not run: gates unavailable.'))
    assert 'full suite not run' in stored['summary']
    assert attempt != limited_attempt


async def test_persistent_helper_a_then_b_blocked_then_b_resumed(api, env, project, model_inventory):
    """Scenario 6: give creates a new attempt; report revisions remain attributable."""
    lead = await ready_lead(api, env, project)
    helper, a, first = await selected_helper(api, env, lead, 'Task A: inspect parser', 'Persistent', kind='persistent')
    result(await tool(api, env, helper, 'report', attempt=first, status='done', summary='A inspected'))
    b = await assign(api, env, lead, 'Task B: implement parser')
    result(await tool(api, env, lead, 'give', helper=helper['id'], assignment=b['id']))
    from acc.server.orchestrator.assignments import Attempt
    from acc.server.orchestrator.delivery import TaskReport
    with api.app.state.db() as db:
        second = db.scalar(select(Attempt).where(Attempt.assignment_id == b['id'])).id
    result(await tool(api, env, helper, 'report', attempt=second, status='blocked', summary='Need delimiter choice'))
    result(await tool(api, env, helper, 'report', attempt=second, status='done', summary='B complete after answer'))
    with api.app.state.db() as db:
        reports = list(db.scalars(select(TaskReport).where(TaskReport.attempt_id == second).order_by(TaskReport.version)))
        assert [r.status for r in reports] == ['blocked', 'done']
        assert reports[0].version < reports[1].version
        assert db.get(Attempt, first).assignment_id == a['id']
        assert db.get(Attempt, second).session_id == helper['id']


async def test_uncertain_start_does_not_duplicate(api, env, project, model_inventory):
    """Lost reply/retry: same selected assignment never creates two sessions or attempts."""
    lead = await ready_lead(api, env, project)
    assignment = await assign(api, env, lead, 'Implement importer once')
    selection = await choose(api, env, lead, assignment)
    helper, attempt = await start(api, env, lead, assignment, selection, 'Once')
    # Discard the successful acknowledgement, as a disconnected caller would.
    reply = await tool(api, env, lead, 'start', assignment=assignment['id'], selection=selection['id'], name='Once')
    assert isinstance(reply.get('ok'), bool)  # adoption or explicit refusal are both allowed
    from acc.server.orchestrator.assignments import Attempt
    with api.app.state.db() as db:
        attempts = list(db.scalars(select(Attempt).where(Attempt.assignment_id == assignment['id'])))
        assert [(a.id, a.session_id) for a in attempts] == [(attempt, helper['id'])]
        members = list(db.scalars(select(Session).where(Session.team_id == lead['id'], Session.parent_session_id == lead['id'])))
        assert [m.id for m in members] == [helper['id']]


async def test_continuation_from_stopped_partial_helper(api, env, project, model_inventory):
    """Owner addition: helper 2 inherits committed work and all earlier attribution."""
    lead = await ready_lead(api, env, project, at_once=1)
    assert callable(getattr(planning, 'draft_brief', None)), 'lane D draft_brief is not present'
    with write_db(api.app.state.db) as db:
        planning.draft_brief(db, lead['id'], '@api', 1, [
            {'text': 'Import CSV locally', 'acceptance': 'A local CSV imports without data loss.'}], [
            {'text': 'Keep customer records on this machine.'}])
        db.commit()
    snapshot = result(await tool(api, env, lead, 'plan'))
    if snapshot['state'] == 'planning':
        result(await tool(api, env, lead, 'plan_ready', reason='Accepted local-only importer scope'))
    assignment = await assign(api, env, lead, 'Deliver the CSV importer and regression checks', requirement_ids=['R-1'])
    first, first_attempt = await start(api, env, lead, assignment, await choose(api, env, lead, assignment), 'Partial')
    with api.app.state.db() as db:
        assert db.get(Session, first['id']).helper_kind == 'temporary'
    root = Path(first['worktree'])
    handoff = f"docs/handoffs/{first['id'][:8]}-partial.md"
    (root / handoff).parent.mkdir(parents=True, exist_ok=True)
    (root / handoff).write_text('Parser implemented; delimiter regression check remains.\n')
    (root / 'parser.txt').write_text('Partial CSV parser\n')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'Save partial importer and handoff')
    head = git_head(root)
    result(await tool(api, env, first, 'report', attempt=first_attempt, status='partial', summary='Parser ready; delimiter check remains'))
    result(await tool(api, env, lead, 'stop', helper=first['id']))
    second, second_attempt = await start(api, env, lead, assignment, await choose(api, env, lead, assignment), 'Continue')
    assert second_attempt != first_attempt and second['id'] != first['id']
    assert git_head(second['worktree']) == head
    assert (Path(second['worktree']) / 'parser.txt').read_text() == 'Partial CSV parser\n'
    text = await launch_text(second, env)
    for evidence in (assignment['goal'], 'R-1', 'Import CSV locally', 'C-1', 'Keep customer records on this machine.',
                     'Parser ready; delimiter check remains', handoff, first['worktree_branch'], head):
        assert evidence.lower() in text
    result(await tool(api, env, second, 'report', attempt=second_attempt, status='done', summary='Importer and regression complete'))
    from acc.server.orchestrator.assignments import Attempt
    from acc.server.orchestrator.delivery import TaskReport
    with api.app.state.db() as db:
        attempts = list(db.scalars(select(Attempt).where(Attempt.assignment_id == assignment['id'])))
        assert {a.session_id for a in attempts} == {first['id'], second['id']}
        reports = list(db.scalars(select(TaskReport).where(TaskReport.attempt_id.in_([first_attempt, second_attempt]))))
        assert {(r.attempt_id, r.status) for r in reports} == {(first_attempt, 'partial'), (second_attempt, 'done')}
        partial = next(r for r in reports if r.attempt_id == first_attempt)
        assert partial.head == head and partial.branch == first['worktree_branch']
        assert partial.handoff_path == handoff


@pytest.mark.parametrize('waiting_state', ['blocked', 'waiting_input'])
async def test_blocked_on_person_is_not_failure(api, env, project, model_inventory, waiting_state):
    """Extra row: waiting changes no failure score (load is held constant)."""
    lead = await ready_lead(api, env, project)
    helper, _, _ = await selected_helper(api, env, lead, 'Inspect input file', 'Waiting')
    assignment = await assign(api, env, lead, 'Compare selection before and after waiting')
    await pause_supervisor(api)
    sup = api.app.state.supervisor
    with write_db(sup.db) as db:
        db.get(Session, helper['id']).observed_state = 'active'
        db.commit()
    before = await sup.team_action(lead['id'], {'action': 'select', 'assignment': assignment['id']})
    with write_db(sup.db) as db:
        db.get(Session, helper['id']).observed_state = waiting_state
        db.commit()
    after = await sup.team_action(lead['id'], {'action': 'select', 'assignment': assignment['id']})
    assert before['chosen'] == after['chosen']
    assert [(c['score'], c['reasons']) for c in before['candidates']] == [
        (c['score'], c['reasons']) for c in after['candidates']]


async def test_assignment_role_requires_matching_granted_profile(api, env, project, model_inventory):
    """Owner addition: no-profile is general only; refusal suggests person-granted profiles."""
    lead = await ready_lead(api, env, project)
    assignment = await assign(api, env, lead, 'Review parser security', task_type='security_review', role='security')
    reply = await tool(api, env, lead, 'select', assignment=assignment['id'])
    # Selection may return a ranked refusal or a tool refusal, but neither may authorize start.
    if reply['ok']:
        output = result(reply)
        assert output['chosen'] is None
        explanation = json.dumps(output).lower()
    else:
        explanation = reply['error'].lower()
    assert 'security' in explanation and 'person' in explanation and 'grant' in explanation


async def test_delegation_accounting(api, env, project, model_inventory):
    """Scenario 10: compiled capabilities disclose native limits and sequential Council fallback.

    Provider-native counts cannot be observed by the fake CLI; do not simulate an LLM
    claiming seven independent voices and call that a qualification.
    """
    lead = await ready_lead(api, env, project, at_once=3)
    text = await launch_text(lead, env)
    assert 'single-session perspectives' in text
    assert 'native' in text and 'parent' in text
    assert 'not shown' in text and ('cannot cap' in text or "can't cap" in text or "cannot be capped" in text)
    assert 'sequential' in text and 'managed' in text
    for index in range(3):
        await selected_helper(api, env, lead, f'Perspective {index + 1}', f'Voice {index + 1}')
    fourth = await assign(api, env, lead, 'Fourth perspective must run sequentially')
    choice = await choose(api, env, lead, fourth)
    refused = await tool(api, env, lead, 'start', assignment=fourth['id'], selection=choice['id'], name='Too many')
    assert not refused['ok'], refused
    await do_setup(api, env)
    response = await api.get(f"/api/teams/{lead['id']}/tree", headers={'Authorization': ''})
    assert response.status_code == 200, response.text
    tree = response.json()
    assert len([member for member in tree['members'] if member['id'] != lead['id']]) == 3


async def test_revisions_and_dependencies(api, env, project):
    """Scenario 8: dropping a prerequisite must not silently authorize dependent work."""
    lead = await ready_lead(api, env, project)
    result(await tool(api, env, lead, 'plan_update', reason='Introduce explicit dependencies', phases=[
        {'key': 'schema', 'title': 'Parser schema'},
        {'key': 'importer', 'title': 'CSV importer', 'depends_on': ['schema']}]))
    result(await tool(api, env, lead, 'plan_update', reason='The schema approach was rejected',
                      phases=[{'key': 'schema', 'status': 'dropped'}]))
    plan = result(await tool(api, env, lead, 'plan'))
    importer = next(phase for phase in plan['phases'] if phase['key'] == 'importer')
    assert 'schema' in importer['blocked_by']
    # Resolving the dependency is an explicit, reasoned plan revision.
    result(await tool(api, env, lead, 'plan_update', reason='Importer will validate raw CSV without a schema',
                      phases=[{'key': 'importer', 'depends_on': []}]))
    plan = result(await tool(api, env, lead, 'plan'))
    assert next(p for p in plan['phases'] if p['key'] == 'importer')['blocked_by'] == []


# D/E Python entry points agreed by the coordinator after the initial checkpoint.
# E's parameter/return envelopes are explicit merge-time assumptions, not HTTP routes.
def brief_service():
    name = 'acc.server.orchestrator.project_briefs'
    assert importlib.util.find_spec(name) is not None, 'lane E project_briefs is not present'
    return importlib.import_module(name)


async def test_planning_modes(api, env, project, model_inventory):
    """Scenario 7: prepare freezes the lead read-only and refuses write assignments.

    The real provider sandbox still needs qualification; this tests its frozen
    launch permission plus server-side refusal, not fake CLI shell enforcement.
    """
    root = Path(project['workspace_path'])
    repo(root)
    response = await api.post('/api/sessions', json={
        'project_id': project['id'], 'provider': 'claude', 'name': 'Prepare only', 'worktree': True,
        'orchestrator': grant(run_mode='prepare', agents=[
            {'agent': 'claude', 'profile': None, 'permission_mode': 'plan'},
            {'agent': 'claude', 'profile': 'fullstack', 'permission_mode': 'acceptEdits'}])})
    assert response.status_code == 201, response.text
    lead = response.json()
    assert lead['launch_spec']['inputs']['permission_mode'] == 'plan'
    assert callable(getattr(planning, 'set_run_mode', None)), 'lane D set_run_mode is not present'
    with write_db(api.app.state.db) as db:
        planning.set_run_mode(db, lead['id'], '@api', 'prepare')
        db.commit()
    result(await tool(api, env, lead, 'plan_update', reason='Prepare a bounded plan', idea='Inspect CSV formats',
                      scope={'in': ['CSV inspection'], 'out': ['Implementation'], 'parked': []},
                      phases=[{'key': 'discovery', 'title': 'Inspect sample CSV'}]))
    result(await tool(api, env, lead, 'plan_ready', reason='Discovery scope is ready'))
    discovery = await assign(api, env, lead, 'Inspect CSV without edits', task_type='discovery',
                             effort='plan', requires=['read_only'])
    helper, _ = await start(api, env, lead, discovery, await choose(api, env, lead, discovery), 'Reader')
    assert helper['launch_spec']['inputs']['permission_mode'] == 'plan'
    implementation = await assign(api, env, lead, 'Implement CSV parsing', role='fullstack', requires=['write'])
    choice = await choose(api, env, lead, implementation)
    refused = await tool(api, env, lead, 'start', assignment=implementation['id'], selection=choice['id'], name='Writer')
    assert not refused['ok'] and 'prepare' in refused['error'].lower(), refused
    # D requires confirmed stops before replacing the actual provider permissions.
    await api.app.state.supervisor.stop(lead['id'], '@api')
    async def confirmed():
        with api.app.state.db() as db:
            row = db.get(Session, lead['id'])
            return row.desired_state == 'stopped' and not row.team_stop_pending
    await wait_for(confirmed)
    with write_db(api.app.state.db) as db:
        planning.set_run_mode(db, lead['id'], '@api', 'build')
        db.commit()
    await api.app.state.supervisor.resume(lead['id'], '@api')
    # A person's stopped-run mode change, rather than an agent's interpretation, unlocks build.
    writer, _ = await start(api, env, lead, implementation, await choose(api, env, lead, implementation), 'Authorized writer')
    assert writer['launch_spec']['inputs']['permission_mode'] == 'acceptEdits'


async def test_reading_constraints_never_authorizes_editing(api, env, project):
    """Scenario 8's authority half: read acknowledgement cannot turn person text into agent-editable text."""
    lead = await ready_lead(api, env, project)
    assert callable(getattr(planning, 'draft_brief', None)), 'lane D draft_brief is not present'
    constraint = {'id': 'C-1', 'text': 'Keep every customer record on this machine.',
                  'source': 'person', 'version': 1, 'state': 'active'}
    requirement = {'id': 'R-1', 'text': 'Import CSV locally', 'acceptance': 'No customer data leaves the machine.',
                   'source': 'brief', 'brief_version': 1}
    with write_db(api.app.state.db) as db:
        planning.draft_brief(db, lead['id'], '@api', 1,
            [{'text': requirement['text'], 'acceptance': requirement['acceptance']}], [{'text': constraint['text']}])
        db.commit()
    before = result(await tool(api, env, lead, 'plan'))
    assert next(c for c in before['constraints'] if c['id'] == 'C-1')['text'] == constraint['text']
    # Also exercise the agreed Python mutator as the lead, not just the tool's
    # argument whitelist. Refusal or a proposal is allowed; changing accepted
    # person text without their decision is not.
    with write_db(api.app.state.db) as db:
        try:
            planning.draft_brief(db, lead['id'], 'session:' + lead['id'], 1, [],
                                 [{'id': 'C-1', 'text': 'Upload customer records to a cloud parser.'}])
        except (worklist.Refused, LaunchRefused):
            db.rollback()
        else:
            db.commit()
    before = result(await tool(api, env, lead, 'plan'))
    assert next(c for c in before['constraints'] if c['id'] == 'C-1')['text'] == constraint['text']
    refused = await tool(api, env, lead, 'plan_update', reason='I read the constraint and prefer cloud processing',
                         constraints=[{**constraint, 'text': 'Upload customer records to a cloud parser.'}])
    assert not refused['ok'], refused
    after = result(await tool(api, env, lead, 'plan'))
    assert after['constraints'] == before['constraints']


async def test_material_decision_blocks_only_named_requirements(api, env, project, model_inventory):
    """The agreed blocks argument persists and gates only the named requirement."""
    lead = await ready_lead(api, env, project)
    assert callable(getattr(planning, 'draft_brief', None)), 'lane D draft_brief is not present'
    requirements = [{'id': f'R-{number}', 'text': text, 'acceptance': text, 'source': 'brief', 'brief_version': 1}
                    for number, text in [(1, 'Import CSV locally'), (2, 'Document existing CSV syntax')]]
    with write_db(api.app.state.db) as db:
        planning.draft_brief(db, lead['id'], '@api', 1,
            [{'text': r['text'], 'acceptance': r['acceptance']} for r in requirements], [])
        card = worklist.add(db, lead['id'], lead['id'], 'session:' + lead['id'], {
            'kind': 'decision', 'title': 'May customer records leave the machine?',
            'options': [{'id': 'local', 'label': 'Local only'}, {'id': 'cloud', 'label': 'Cloud parser'}]}, blocks=['R-1'])
        card_id = card.id
        db.commit()
    cards = (await api.get(f"/api/teams/{lead['id']}/worklist")).json()['items']
    assert next(c for c in cards if c['id'] == card_id)['blocks'] == ['R-1']
    blocked = await assign(api, env, lead, 'Import records', requirement_ids=['R-1'])
    choice = await choose(api, env, lead, blocked)
    refused = await tool(api, env, lead, 'start', assignment=blocked['id'], selection=choice['id'], name='Blocked importer')
    assert not refused['ok'] and ('R-1' in refused['error'] or str(card_id) in refused['error']), refused
    independent = await assign(api, env, lead, 'Document syntax', requirement_ids=['R-2'])
    helper, _ = await start(api, env, lead, independent, await choose(api, env, lead, independent), 'Unblocked docs')
    assert helper['desired_state'] == 'running'


async def test_intake_quality(api, env, project, tmp_path):
    """Complete intake is immutable; an unreadable reference gets one targeted decision."""
    import base64

    from test_project_briefs import person
    api = await person(api, env)
    briefs = brief_service()
    submitted = {'product': 'A local CSV importer', 'people_problem': 'Warehouse staff import stock counts.',
        'journeys': 'Choose CSV, preview errors, import valid rows.', 'first_release': 'Local CSV only.',
        'later': 'No cloud processing or scheduled imports.', 'success': 'A valid CSV imports without data loss.',
        'design': 'Use the existing interface.', 'constraints': 'Keep all customer data local.',
        'uncertainty': 'None about the workflow.', 'run_mode': 'prepare'}
    upload = await api.post(f'/api/project-brief-references/{project["id"]}', json={
        'caption': 'Table spacing study', 'guidance': 'Layout evidence only; no new requirements.',
        'media_type': 'image/png', 'data': base64.b64encode(b'\x89PNG\r\n\x1a\nnot a decodable image').decode()})
    assert upload.status_code == 200, upload.text
    saved = await api.post(f'/api/projects/{project["id"]}/brief', json={
        'version': 0, 'fields': submitted, 'references': ['REF-1']})
    assert saved.status_code == 200, saved.text
    repo(Path(project['workspace_path']))
    launched = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude',
        'name': 'Brief acceptance', 'worktree': True, 'orchestrator': grant(brief_version=1)})
    assert launched.status_code == 201, launched.text
    lead = launched.json()
    with write_db(api.app.state.db) as db:
        assert briefs.read_for_team(db, lead['id'])['submitted'] == submitted
        briefs.interpret(db, lead['id'], {'brief_version': 1, 'text': 'Local CSV import; no guessed visual details.',
            'references': {'REF-1': {'status': 'unreadable', 'note': 'I cannot read REF-1; please provide a readable image.'}}})
        briefs.draft_planning(db, lead['id'], 'session:' + lead['id'], 1,
            [{'text': 'Import CSV locally', 'acceptance': 'Valid rows import without cloud uploads.'}],
            [{'text': 'Keep all customer data local.'}])
        briefs.question(db, lead['id'], {'field': 'REF-1', 'question': 'Could you replace or omit REF-1?',
            'reason': 'R-1 needs a readable spacing reference.', 'blocks': ['R-1'],
            'options': [{'id': 'replace', 'label': 'Provide a readable reference'}, {'id': 'omit', 'label': 'Omit the reference'}],
            'recommendation': {'option': 'replace', 'reason': 'Preserve your intended spacing without inventing it.'}})
        db.commit()
    with api.app.state.db() as db:
        after = briefs.read_for_team(db, lead['id'])
        assert after['submitted'] == submitted
        reference = after['interpretations'][-1]['references']['REF-1']
        assert reference['status'] == 'unreadable' and 'cannot read' in reference['note']
        questions = [item for item in worklist.snapshot(db, lead['id'])['items']
                     if item['kind'] == 'decision' and item['state'] == 'open' and 'Brief v1,' in item['steps']]
        assert questions and all('REF-1' in item['title'] + item['steps'] for item in questions)
        assert all(item['blocks'] == ['R-1'] for item in questions)
