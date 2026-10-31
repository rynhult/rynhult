"""Lane C: immutable classification, hard requirements and attempt continuity."""
import pytest

from acc.server.orchestrator import selection
from acc.server.supervisor_common import LaunchRefused


def ranked(*, requires=(), role='backend', models=None, pin=None, profile='backend'):
    allowed = [{'agent': 'claude', 'profile': profile, 'roles': ['backend'],
                'inputs': {'skills': ['python'], 'mcps': ['docs'], 'permission_mode': 'acceptEdits'}}]
    inventory = {('local', 'claude'): {'logged_in': True, 'models': models or [{'id': 'model-a'}]}}
    return selection.rank({'task_type': 'implementation', 'role': role, 'requires': list(requires)},
                          allowed, ['local'], inventory, pin=pin)


@pytest.mark.parametrize('requirement', ['image_input', 'tool:missing', 'read_only', 'network:none'])
def test_missing_hard_requirement_cannot_be_scored_away(requirement):
    result = ranked(requires=[requirement])
    assert result['chosen'] is None
    assert any(requirement in why for why in result['candidates'][0]['rejections'])


def test_image_capability_must_be_explicit_and_same_snapshot_is_deterministic():
    args = {'requires': ['image_input'], 'models': [{'id': 'model-a', 'image_input': True}]}
    assert ranked(**args)['chosen'] == 0
    assert ranked(**args) == ranked(**args)


def test_role_is_a_filter_and_no_profile_only_matches_general():
    assert ranked(role='security')['chosen'] is None
    assert ranked(role='general', profile=None)['chosen'] == 0
    assert ranked(role='backend', profile=None)['chosen'] is None


def test_manual_pin_cannot_escape_ranked_grant_or_task_policy():
    assert ranked(pin={'agent': 'copilot'})['chosen'] is None
    assert ranked(pin={'agent': 'claude', 'model': 'not-granted'})['chosen'] is None
    assert ranked(pin={'agent': 'claude'})['chosen'] == 0


def test_languages_are_removed_and_classification_role_is_required():
    for value in ({'task_type': 'implementation'}, {'task_type': 'implementation', 'role': 'backend', 'languages': ['python']}):
        with pytest.raises(LaunchRefused):
            selection.validate_need(value)


@pytest.fixture
def runtime(monkeypatch):
    monkeypatch.setenv('ACC_ORCHESTRATOR', '1')
    monkeypatch.setenv('ACC_ORCHESTRATOR_V2_DELIVERY', '1')

    async def ready(supervisor, account, machines, agents):
        return {(machine, agent): {'logged_in': True, 'models': [{'id': 'model-a'}]}
                for machine in machines for agent in agents}
    monkeypatch.setattr(selection, 'readiness', ready)


async def assigned(api, env, project, *, goal='Implement a bounded change', task_type='implementation', **need):
    from test_team import lead, request
    caller = await lead(api, project, env)
    response = await request(api, env, caller, 'assign', goal=goal, task_type=task_type,
                             acceptance=['Related tests pass'], requirement_ids=[],
                             need={'role': 'general', 'classification_reason': 'Small bounded implementation', **need})
    assert response['ok'], response
    return caller, response['result']


async def test_assignment_selection_start_replay_and_versioned_report(runtime, api, env, project):
    from test_team import request
    caller, assignment = await assigned(api, env, project)
    selected = await request(api, env, caller, 'select', assignment=assignment['id'])
    assert selected['ok'], selected
    selected = selected['result']
    assert selected['classification']['role'] == 'general'
    assert selected['catalog_revision'] and selected['profile_digest']
    start = dict(assignment=assignment['id'], selection=selected['id'], name='Worker')
    first = await request(api, env, caller, 'start', **start)
    assert first['ok'], first
    helper = first['result']
    again = await request(api, env, caller, 'start', **start)
    assert again['ok'] and again['result']['id'] == helper['id'], again
    partial = await request(api, env, helper, 'report', status='partial', summary='First part', findings_md='# Evidence\nRead-only findings.')
    assert partial['ok'], partial
    done = await request(api, env, helper, 'report', status='done', summary='Finished')
    assert done['ok'] and done['result']['version'] == 2, done
    assert done['result']['attempt_id'] == partial['result']['attempt_id']


async def test_v2_plain_start_is_refused(runtime, api, env, project):
    from test_team import lead, request
    caller = await lead(api, project, env)
    response = await request(api, env, caller, 'start', agent='claude', name='Bypass', task='No classification')
    assert not response['ok'] and 'assignment' in response['error'].lower()


@pytest.mark.parametrize('state,expected', [('blocked', 0), ('waiting_input', 0), ('failed', 1), ('idle', 0)])
def test_only_failure_has_provider_penalty(state, expected):
    from types import SimpleNamespace

    from acc.server.models import utcnow
    at = utcnow()
    assert selection.failure_weight(SimpleNamespace(observed_state=state, ended_at=None, created_at=at), at) == expected


async def test_partial_attempt_continues_from_committed_head(runtime, api, env, project):
    from pathlib import Path

    from test_changes import git
    from test_team import request
    caller, assignment = await assigned(api, env, project)
    picked = (await request(api, env, caller, 'select', assignment=assignment['id']))['result']
    first = await request(api, env, caller, 'start', assignment=assignment['id'], selection=picked['id'], name='First')
    assert first['ok'], first
    helper = first['result']
    root = Path(helper['worktree'])
    (root / 'continued.txt').write_text('First part committed')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'First part')
    report = await request(api, env, helper, 'report', status='partial', summary='Continue the second part', findings_md='# First attempt evidence')
    assert report['ok'], report
    (root / 'last-commit.txt').write_text('Committed after the partial report')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'Final stopped task head')
    stopped_head = git(root, 'rev-parse', 'HEAD')
    assert (await request(api, env, caller, 'stop', helper=helper['id']))['ok']
    picked = (await request(api, env, caller, 'select', assignment=assignment['id']))['result']
    second = await request(api, env, caller, 'start', assignment=assignment['id'], selection=picked['id'], name='Second')
    assert second['ok'], second
    next_helper = second['result']
    assert (Path(next_helper['worktree']) / 'continued.txt').read_text() == 'First part committed'
    assert (Path(next_helper['worktree']) / 'last-commit.txt').read_text() == 'Committed after the partial report'
    from acc.server.orchestrator.assignments import Attempt
    with api.app.state.db() as db:
        attempt = db.get(Attempt, next_helper['id'])
        previous = attempt.brief['earlier_attempts'][0]
        assert previous['session'] == helper['id']
        assert previous['latest_report']['summary'] == 'Continue the second part'
        assert previous['head'] == stopped_head
        assert previous['latest_report']['head'] == report['result']['head']
    inherited = await request(api, env, next_helper, 'read', helper=helper['id'], attempt=report['result']['attempt_id'], version=1)
    assert inherited['ok'] and inherited['result']['reports'][0]['findings_md'] == '# First attempt evidence', inherited
    second_root = Path(next_helper['worktree'])
    (second_root / 'second-part.txt').write_text('Second part committed')
    git(second_root, 'add', '.')
    git(second_root, 'commit', '-qm', 'Second part')
    final = await request(api, env, next_helper, 'report', status='done', summary='Finished both parts')
    assert final['ok'], final
    assert final['result']['attempt_id'] != report['result']['attempt_id']
    assert (await request(api, env, caller, 'stop', helper=next_helper['id']))['ok']
    earlier_merge = await request(api, env, caller, 'merge', helper=helper['id'])
    assert earlier_merge['ok'] and earlier_merge['result']['merged'], earlier_merge
    from acc.server.orchestrator.assignments import Assignment
    with api.app.state.db() as db:
        assert db.get(Assignment, assignment['id']).state == 'reported'  # second head is not merged yet
    merged = await request(api, env, caller, 'merge', helper=next_helper['id'])
    assert merged['ok'] and merged['result']['merged'], merged
    with api.app.state.db() as db:
        assert db.get(Attempt, helper['id']).merged
        assert db.get(Attempt, next_helper['id']).merged



async def test_uncertain_attempt_never_makes_a_second_checkout(runtime, api, env, project, monkeypatch):
    from test_team import request

    from acc.server.orchestrator.assignments import Attempt
    caller, assignment = await assigned(api, env, project)
    picked = (await request(api, env, caller, 'select', assignment=assignment['id']))['result']
    supervisor = api.app.state.supervisor
    calls = []
    original = supervisor.broker.call

    async def uncertain(operation, **kwargs):
        if operation == 'make_worktree':
            calls.append(kwargs)
            raise RuntimeError('synthetic uncertain broker reply')
        return await original(operation, **kwargs)
    monkeypatch.setattr(supervisor.broker, 'call', uncertain)
    args = dict(assignment=assignment['id'], selection=picked['id'], name='Uncertain')
    first = await request(api, env, caller, 'start', **args)
    second = await request(api, env, caller, 'start', **args)
    assert not first['ok'] and not second['ok'] and len(calls) == 1
    assert 'uncertain launch' in second['error']
    with api.app.state.db() as db:
        from sqlalchemy import select
        attempt = db.scalar(select(Attempt).where(Attempt.assignment_id == assignment['id']))
        # No session was created: the attempt is failed (not stuck launching) and its intended checkout recorded.
        assert attempt.state == 'failed' and attempt.checkout['path'].endswith('/.worktrees/team-' + attempt.id)


async def test_persistent_helper_second_assignment_and_blocked_resume(runtime, api, env, project):
    from helpers import write_db
    from test_orchestrator_selection_api import human
    from test_team import request

    from acc.server.models import Session
    headers = await human(api, env)
    caller, assignment = await assigned(api, env, project)
    chosen = (await request(api, env, caller, 'select', assignment=assignment['id']))['result']
    started = await request(api, env, caller, 'start', assignment=assignment['id'], selection=chosen['id'], name='Persistent', kind='persistent')
    assert started['ok'], started
    helper = started['result']
    first = await request(api, env, helper, 'report', status='done', summary='A complete')
    assert first['ok'], first
    tree = (await api.get(f"/api/teams/{caller['id']}/tree", headers=headers)).json()
    assert next(m for m in tree['members'] if m['id'] == helper['id'])['report_status'] == 'done'
    next_task = await request(api, env, caller, 'assign', goal='Second task', task_type='implementation',
                              acceptance=[], requirement_ids=[], need={'role':'general', 'classification_reason':'Same small role'})
    assert next_task['ok'], next_task
    with write_db(api.app.state.db) as db:
        db.get(Session, helper['id']).observed_state = 'idle'
        db.commit()
    given = await request(api, env, caller, 'give', helper=helper['id'], assignment=next_task['result']['id'])
    assert given['ok'], given
    tree = (await api.get(f"/api/teams/{caller['id']}/tree", headers=headers)).json()
    assert next(m for m in tree['members'] if m['id'] == helper['id'])['report_status'] is None
    from acc.server.orchestrator import delivery
    from acc.server.orchestrator.assignments import Attempt
    # Simulate the outbox receipt boundary (the delivery suite exercises hook ACKs).
    pending = await request(api, env, helper, 'report', status='done', summary='Late A default')
    assert not pending['ok'] and 'not acknowledged' in pending['error']
    with write_db(api.app.state.db) as db:
        attempt = db.get(Attempt, given['result']['attempt'])
        db.get(delivery.MessageDelivery, attempt.brief['delivery_id']).state = 'acknowledged'
        db.commit()
    with api.app.state.db() as db:
        brief = db.get(Attempt, given['result']['attempt']).brief
        assert brief['attempt_id'] == given['result']['attempt'] and brief['attempt_brief_version'] == 1
    # A handover to a main with a behind wall clock must not revive task A.
    from datetime import timedelta
    with write_db(api.app.state.db) as db:
        earlier = db.get(Attempt, first['result']['attempt_id'])
        db.get(Attempt, given['result']['attempt']).created_at = earlier.created_at - timedelta(hours=1)
        db.commit()
    blocked = await request(api, env, helper, 'report', status='blocked', summary='Waiting for the person')
    assert blocked['ok'], blocked
    assert blocked['result']['attempt_id'] == given['result']['attempt']
    resumed = await request(api, env, helper, 'report', status='done', summary='B complete')
    assert resumed['ok'] and resumed['result']['version'] == 2, resumed
    assert resumed['result']['attempt_id'] == blocked['result']['attempt_id']
    assert first['result']['attempt_id'] != resumed['result']['attempt_id']
    with write_db(api.app.state.db) as db:
        from sqlalchemy import select
        current_report = db.scalar(select(delivery.TaskReport).where(delivery.TaskReport.attempt_id == given['result']['attempt'], delivery.TaskReport.version == 2))
        current_report.received_at = earlier.created_at - timedelta(hours=2)
        db.commit()
    shown = (await api.get(f"/api/sessions/{helper['id']}/report")).json()['report']
    assert shown['attempt_id'] == given['result']['attempt'] and shown['version'] == 2


async def test_read_only_reviewer_persists_findings_without_writing_repo(runtime, api, env, project):
    from pathlib import Path

    from test_team import lead, request
    caller = await lead(api, project, env, agents=[{'agent':'claude', 'profile':'security'}])
    assignment = await request(api, env, caller, 'assign', goal='Review only', task_type='security_review', acceptance=[],
                               requirement_ids=[], need={'role':'security', 'requires':['read_only'],
                               'author_session':caller['id'], 'classification_reason':'Independent read-only security review'})
    assert assignment['ok'], assignment
    picked = (await request(api, env, caller, 'select', assignment=assignment['result']['id']))['result']
    assert picked['chosen'] is not None, picked
    first = await request(api, env, caller, 'start', assignment=assignment['result']['id'], selection=picked['id'], name='Review')
    assert first['ok'], first
    reviewer = first['result']
    assert reviewer['id'] != caller['id']
    details = (await api.get(f"/api/sessions/{reviewer['id']}")).json()
    assert details['launch_spec']['inputs']['permission_mode'] == 'plan'
    findings = '# Findings\n' + 'x' * 65000
    from test_changes import git
    reviewed = {'reviewed_branch': caller['worktree_branch'], 'reviewed_head': git(Path(caller['worktree']), 'rev-parse', 'HEAD').strip()}
    report = await request(api, env, reviewer, 'report', status='done', summary='Review complete', findings_md=findings, **reviewed)
    assert report['ok'] and report['result']['findings_md'] == findings, report
    revised = await request(api, env, reviewer, 'report', status='done', summary='Review revised', findings_md='Kept findings', **reviewed)
    assert revised['ok'] and revised['result']['version'] == 2, revised
    from acc.server.orchestrator import assignments
    with api.app.state.db() as db:
        versions = assignments.reports(db, report['result']['attempt_id'])
        assert len(versions) == 2
        assert all(row.reviewed_head == reviewed['reviewed_head'] and row.reviewed_branch == reviewed['reviewed_branch'] for row in versions)
    assert not list(Path(reviewer['worktree']).rglob('*findings*'))
    invalid = await request(api, env, reviewer, 'report', status='done', summary='Too large', findings_md='é' * 33000)
    assert not invalid['ok']
    foreign = await request(api, env, caller, 'report', attempt=report['result']['attempt_id'], status='done', summary='Forged')
    assert not foreign['ok']
    read = await request(api, env, caller, 'read', helper=reviewer['id'], attempt=report['result']['attempt_id'], version=1)
    assert read['ok'] and read['result']['reports'][0]['findings_md'] == findings, read


def test_pin_for_docs_provider_cannot_bypass_security_task_policy():
    from test_orchestrator_selection import choices, inventory
    result = selection.rank({'task_type':'security_review', 'role':'backend'}, choices(), ['local'], inventory(), pin={'agent':'copilot'})
    assert result['chosen'] is None
    assert all(not c['eligible'] for c in result['candidates'])


async def test_lost_reply_after_session_commit_adopts_and_keeps_owned_checkout(runtime, api, env, project, monkeypatch):
    from pathlib import Path

    from sqlalchemy import select
    from test_team import request

    from acc.server.orchestrator.assignments import Attempt
    caller, assignment = await assigned(api, env, project)
    selected = (await request(api, env, caller, 'select', assignment=assignment['id']))['result']
    supervisor = api.app.state.supervisor
    real = supervisor.create_session
    launched = []

    async def lost_reply(*args, **kwargs):
        result = await real(*args, **kwargs)
        launched.append(result)
        raise RuntimeError('synthetic reply lost after session commit')
    monkeypatch.setattr(supervisor, 'create_session', lost_reply)
    args = dict(assignment=assignment['id'], selection=selected['id'], name='Reply lost')
    assert not (await request(api, env, caller, 'start', **args))['ok']
    with supervisor.db() as db:
        attempt = db.scalar(select(Attempt).where(Attempt.assignment_id == assignment['id']))
        assert attempt.session_id == launched[0]
    again = await request(api, env, caller, 'start', **args)
    assert again['ok'] and again['result']['id'] == launched[0], again
    assert len(launched) == 1 and Path(again['result']['worktree']).is_dir()


async def test_missing_report_collection_is_per_attempt_not_per_session(runtime, api, env, project, monkeypatch):
    from helpers import write_db
    from sqlalchemy import select
    from test_team import request

    from acc.server.models import Session
    from acc.server.orchestrator import assignments, delivery
    caller, assignment = await assigned(api, env, project)
    selected = (await request(api, env, caller, 'select', assignment=assignment['id']))['result']
    helper = (await request(api, env, caller, 'start', assignment=assignment['id'], selection=selected['id'], name='Persistent', kind='persistent'))['result']
    assert (await request(api, env, helper, 'report', status='done', summary='A complete'))['ok']
    next_task = await request(api, env, caller, 'assign', goal='Second task', task_type='implementation',
                              acceptance=[], requirement_ids=[], need={'role':'general','classification_reason':'Same bounded role'})
    with write_db(api.app.state.db) as db:
        db.get(Session, helper['id']).observed_state = 'idle'
        db.commit()
    given = await request(api, env, caller, 'give', helper=helper['id'], assignment=next_task['result']['id'])
    assert given['ok'], given
    attempt_id = given['result']['attempt']
    async def metadata(*args):
        return {'branch':helper['worktree_branch'], 'head':'a'*40, 'handoff_path':f'docs/handoffs/demo-{attempt_id[:8]}-work.md'}
    monkeypatch.setattr(delivery, 'report_metadata', metadata)
    with write_db(api.app.state.db) as db:
        db.get(Session, helper['id']).observed_state = 'stopped'
        db.get(Session, helper['id']).desired_state = 'stopped'
        db.commit()
    await delivery.collect_missing_reports(api.app.state.supervisor)
    with api.app.state.db() as db:
        report = db.scalar(select(delivery.TaskReport).where(delivery.TaskReport.attempt_id == attempt_id))
        assert report is not None and report.version == 1 and report.status == 'partial'
        assert assignments.current(db, helper['id']).id == attempt_id


async def test_unknown_catalog_role_is_refused_before_creating_assignment(runtime, api, env, project):
    from test_team import lead, request
    caller = await lead(api, project, env)
    answer = await request(api, env, caller, 'assign', goal='Bounded task', task_type='implementation', acceptance=[],
                           requirement_ids=[], need={'role':'not-a-catalog-role','classification_reason':'Unknown role'})
    assert not answer['ok'] and 'catalog role' in answer['error']


async def test_profile_revision_cannot_be_refreshed_by_override(runtime, api, env, project, monkeypatch):
    from test_orchestrator_selection_api import human
    from test_team import request
    headers = await human(api, env)
    caller, assignment = await assigned(api, env, project)
    row = (await request(api, env, caller, 'select', assignment=assignment['id']))['result']
    monkeypatch.setattr(api.app.state.supervisor.catalog, 'revision', 'synthetic-new-revision')
    changed = await api.post(f"/api/teams/{caller['id']}/selections/{row['id']}/override", headers=headers,
                             json={'choice':row['chosen'],'reason':'A stale tab cannot refresh qualification'})
    assert changed.status_code == 409 and 'catalog changed' in changed.text
    refused = await request(api, env, caller, 'start', assignment=assignment['id'], selection=row['id'], name='Stale')
    assert not refused['ok'] and 'catalog changed' in refused['error']


async def test_requirement_change_keeps_revalidation_even_after_late_report(runtime, api, env, project):
    from helpers import write_db
    from sqlalchemy import select
    from test_team import request

    from acc.server.orchestrator import assignments, delivery
    caller, assignment = await assigned(api, env, project)
    selected = (await request(api, env, caller, 'select', assignment=assignment['id']))['result']
    helper = (await request(api, env, caller, 'start', assignment=assignment['id'], selection=selected['id'], name='Worker'))['result']
    assert (await request(api, env, helper, 'report', status='done', summary='Old scope complete'))['ok']
    with write_db(api.app.state.db) as db:
        row = db.get(assignments.Assignment, assignment['id'])
        row.requirement_ids, row.state = ['R-1'], 'verified'  # lane D's accepted requirement binding
        affected = assignments.invalidate(db, caller['id'], ['R-1'], notice='Synthetic accepted requirement changed', version=2,
                                       supervisor=api.app.state.supervisor)
        assert affected == [assignment['id']] and row.state == 'needs_revalidation'
        notice = db.scalar(select(delivery.MessageDelivery).where(delivery.MessageDelivery.attempt_id == helper['id']))
        assert notice is not None and 'version 2' in notice.body
        db.commit()
    late = await request(api, env, helper, 'report', status='done', summary='Late old evidence')
    assert late['ok'], late
    with api.app.state.db() as db:
        assert db.get(assignments.Assignment, assignment['id']).state == 'needs_revalidation'


def test_required_tool_comes_from_frozen_profile():
    assert ranked(requires=['tool:docs'])['chosen'] == 0


async def test_attempts_with_same_short_prefix_have_distinct_terminal_identities(runtime, api, env, project, monkeypatch):
    from types import SimpleNamespace

    from test_team import request

    from acc.server.orchestrator import assignments
    caller, first = await assigned(api, env, project)
    second = await request(api, env, caller, 'assign', goal='Another bounded task', task_type='implementation',
                            acceptance=[], requirement_ids=[], need={'role':'general','classification_reason':'Independent task'})
    selected_a = (await request(api, env, caller, 'select', assignment=first['id']))['result']
    selected_b = (await request(api, env, caller, 'select', assignment=second['result']['id']))['result']
    identities = iter(['12345678' + 'a'*24, '12345678' + 'b'*24])
    monkeypatch.setattr(assignments, 'uuid', SimpleNamespace(uuid4=lambda: SimpleNamespace(hex=next(identities))))
    one = await request(api, env, caller, 'start', assignment=first['id'], selection=selected_a['id'], name='Worker')
    two = await request(api, env, caller, 'start', assignment=second['result']['id'], selection=selected_b['id'], name='Worker')
    assert one['ok'] and two['ok'], (one, two)
    assert one['result']['tmux_name'] != two['result']['tmux_name']
    refused = await request(api, env, two['result'], 'read', helper=one['result']['id'], attempt=one['result']['id'])
    assert not refused['ok']


async def test_legacy_grant_keeps_legacy_start_after_global_v2_enable(runtime, api, env, project, monkeypatch):
    from test_team import lead, request
    with monkeypatch.context() as before:
        before.setenv('ACC_ORCHESTRATOR', '0')
        legacy = await lead(api, project, env)
    started = await request(api, env, legacy, 'start', name='Legacy helper', agent='claude', task='Original grant task')
    assert started['ok'], started


async def test_v2_select_requires_an_assignment_not_an_untracked_task(runtime, api, env, project):
    from test_team import lead, request
    caller = await lead(api, project, env)
    result = await request(api, env, caller, 'select', task='Untracked work',
                           need={'role':'general','task_type':'implementation','classification_reason':'Bypass attempt'})
    assert not result['ok']


async def test_older_broker_cannot_silently_ignore_continuation_base(runtime, api, env, project, monkeypatch):
    from sqlalchemy import func, select
    from test_team import request

    from acc.server.models import Session
    caller, assignment = await assigned(api, env, project)
    chosen = (await request(api, env, caller, 'select', assignment=assignment['id']))['result']
    helper = (await request(api, env, caller, 'start', assignment=assignment['id'], selection=chosen['id'], name='First'))['result']
    assert (await request(api, env, helper, 'report', status='partial', summary='Continue this work'))['ok']
    assert (await request(api, env, caller, 'stop', helper=helper['id']))['ok']
    picked = (await request(api, env, caller, 'select', assignment=assignment['id']))['result']
    supervisor = api.app.state.supervisor
    real = supervisor.broker.call
    async def old_broker(operation, **kwargs):
        if operation == 'make_worktree':
            kwargs.pop('base_commit', None)
        return await real(operation, **kwargs)
    monkeypatch.setattr(supervisor.broker, 'call', old_broker)
    refused = await request(api, env, caller, 'start', assignment=assignment['id'], selection=picked['id'], name='Unsupported broker')
    assert not refused['ok'] and 'did not confirm the continuation base' in refused['error'], refused
    with supervisor.db() as db:
        assert db.scalar(select(func.count()).select_from(Session).where(Session.team_id == caller['id'])) == 2


async def test_assignment_pages_keep_older_history_accessible(runtime, api, env, project):
    import uuid

    from helpers import write_db
    from test_orchestrator_selection_api import human
    from test_team import lead

    from acc.server.orchestrator.assignments import Assignment
    headers = await human(api, env)
    caller = await lead(api, project, env)
    with write_db(api.app.state.db) as db:
        for index in range(26):
            db.add(Assignment(id=uuid.uuid4().hex, team_id=caller['id'], created_by=caller['id'],
                task_type='implementation', goal=f'Synthetic task {index}', need={'role':'general'}, state='open'))
        db.commit()
    first = await api.get(f"/api/teams/{caller['id']}/selections", headers=headers)
    last = await api.get(f"/api/teams/{caller['id']}/selections?assignment_offset=25", headers=headers)
    assert first.status_code == last.status_code == 200
    assert len(first.json()['assignments']) == 25 and len(last.json()['assignments']) == 1
    assert first.json()['assignments_total'] == last.json()['assignments_total'] == 26
    assert last.json()['assignments'][0]['id'] not in {v['id'] for v in first.json()['assignments']}


def test_planning_seam_prepare_is_limited_to_read_only_discovery():
    from types import SimpleNamespace

    from acc.server.orchestrator import assignments
    class DB:
        def get(self, *_):
            return SimpleNamespace(run_mode='prepare', version=3)

        def scalars(self, *_):
            return []
    row = assignments.Assignment(team_id='a'*32, task_type='implementation', need={'requires':['write']}, requirement_ids=[])
    with pytest.raises(LaunchRefused, match='read-only discovery'):
        assignments.planning_context(DB(), row, starting=True)
    row.task_type, row.need = 'discovery', {'requires':['read_only']}
    assert assignments.planning_context(DB(), row, starting=True)['run_mode'] == 'prepare'


def test_planning_seam_unknown_requirements_and_material_cards_fail_closed(monkeypatch):
    from types import SimpleNamespace

    from acc.server.orchestrator import assignments, planning
    # Lane D is independently stacked: exercise the shared mapped-table interface
    # without defining a competing Requirement table or migration in this lane.
    monkeypatch.setattr(planning, 'Requirement', assignments.Assignment, raising=False)
    requirement = assignments.Assignment(id='R-1', state='active')
    monkeypatch.setattr(planning, 'Constraint', None)
    card = SimpleNamespace(id='decision-1', title='Choose the data boundary', blocks=['R-1'])
    class DB:
        def get(self, *_):
            return SimpleNamespace(run_mode='build', version=4)

        def scalars(self, query):
            return [requirement] if query.column_descriptions[0]['entity'] is assignments.Assignment else [card]
    row = assignments.Assignment(team_id='a'*32, task_type='implementation', need={'requires':[]}, requirement_ids=['R-2'])
    with pytest.raises(LaunchRefused, match='unknown requirements'):
        assignments.planning_context(DB(), row, starting=True)
    row.requirement_ids = ['R-1']
    with pytest.raises(LaunchRefused, match='decision-1: Choose the data boundary'):
        assignments.planning_context(DB(), row, starting=True)
    card.blocks = ['R-2']
    assert assignments.planning_context(DB(), row, starting=True)['requirements'][0]['id'] == 'R-1'


async def test_scope_change_is_accepted_even_with_delivery_off(runtime, api, env, project, monkeypatch):
    from helpers import write_db
    from test_team import request

    from acc.server.orchestrator import assignments
    caller, assignment = await assigned(api, env, project)
    selected = (await request(api, env, caller, 'select', assignment=assignment['id']))['result']
    assert (await request(api, env, caller, 'start', assignment=assignment['id'], selection=selected['id'], name='Active'))['ok']
    monkeypatch.setenv('ACC_ORCHESTRATOR_V2_DELIVERY', '0')
    with write_db(api.app.state.db) as db:
        assert assignments.invalidate(db, caller['id'], notice='Changed scope', version=2, supervisor=api.app.state.supervisor) == [assignment['id']]
        assert db.get(assignments.Assignment, assignment['id']).state == 'needs_revalidation'
        db.commit()


async def test_give_rechecks_planning_after_idle_probe(runtime, api, env, project, monkeypatch):
    from helpers import pause_supervisor, write_db
    from sqlalchemy import select
    from test_team import request

    from acc.server.models import Session
    from acc.server.orchestrator import assignments, delivery, planning, worklist
    caller, assignment = await assigned(api, env, project)
    chosen = (await request(api, env, caller, 'select', assignment=assignment['id']))['result']
    started = await request(api, env, caller, 'start', assignment=assignment['id'], selection=chosen['id'],
                            name='Persistent', kind='persistent')
    assert started['ok'], started
    helper = started['result']
    assert (await request(api, env, helper, 'report', status='done', summary='First task complete'))['ok']
    with write_db(api.app.state.db) as db:
        planning.draft_brief(db, caller['id'], '@api', 1, [{'text': 'Import CSV'}], [])
        db.get(Session, helper['id']).observed_state = 'idle'
        db.commit()
    next_task = await request(api, env, caller, 'assign', goal='Import CSV', task_type='implementation',
        acceptance=[], requirement_ids=['R-1'], need={'role': 'general', 'classification_reason': 'Same bounded role'})
    assert next_task['ok'], next_task
    await pause_supervisor(api)
    async def idle(*args, **kwargs):
        with write_db(api.app.state.db) as db:
            worklist.add(db, caller['id'], caller['id'], 'session:' + caller['id'], {
                'kind': 'decision', 'title': 'Confirm import privacy',
                'options': [{'id': 'yes', 'label': 'Yes'}, {'id': 'no', 'label': 'No'}]}, blocks=['R-1'])
            db.commit()
        return True
    monkeypatch.setattr(delivery, 'safety', idle)
    with pytest.raises(LaunchRefused, match='blocked by decision'):
        await api.app.state.supervisor.team_action(caller['id'], {'action': 'give',
            'helper': helper['id'], 'assignment': next_task['result']['id']})
    with api.app.state.db() as db:
        assert not list(db.scalars(select(assignments.Attempt).where(
            assignments.Attempt.assignment_id == next_task['result']['id'])))


@pytest.mark.parametrize('agent,inputs,expected', [
    ('claude', {'permission_mode': 'plan'}, 'read-only'),
    ('codex', {'sandbox': 'read-only'}, 'read-only'),
    ('copilot', {'options': {'mode': 'plan'}}, 'read-only'),
    ('copilot', {'options': {'mode': 'plan', 'permissions': 'ask'}}, 'read-only'),
    ('copilot', {'options': {'mode': 'plan', 'permissions': 'all'}}, 'implement'),
    ('copilot', {'options': {'mode': 'plan', 'permissions': 'tools'}}, 'implement'),
    ('cursor', {'options': {'mode': 'plan', 'force': True}}, 'implement'),
    ('cursor', {'options': {'mode': 'ask'}}, 'read-only'),
    ('antigravity', {'options': {'mode': 'plan', 'skip_permissions': True}}, 'implement'),
    ('claude', {'permission_mode': 'plan', 'extra_args': ['--allowedTools', 'Bash']}, 'implement'),
    ('opencode', {'options': {'mode': 'plan'}}, 'implement'),
])
def test_read_only_means_no_write_everything_option(agent, inputs, expected):
    assert selection.permission({'agent': agent, 'inputs': inputs}) == expected


def test_a_read_only_requirement_never_selects_a_plan_mode_that_can_write():
    inventory = {('local', 'copilot'): {'logged_in': True, 'models': [{'id': 'model-a'}]}}
    def rank(options):
        allowed = [{'agent': 'copilot', 'profile': None, 'inputs': {'options': options}}]
        return selection.rank({'task_type': 'discovery', 'role': 'general', 'requires': ['read_only']},
                              allowed, ['local'], inventory)
    assert rank({'mode': 'plan', 'permissions': 'ask'})['chosen'] == 0
    refused = rank({'mode': 'plan', 'permissions': 'all'})
    assert refused['chosen'] is None
    assert any('read_only' in why for why in refused['candidates'][0]['rejections'])


async def test_findings_goal_and_acceptance_lose_bidi_zero_width_and_control_characters(runtime, api, env, project):
    from test_team import request

    from acc.server.orchestrator import delivery
    hidden = 'Approve‮evil​\x1b[31m'
    caller, assignment = await assigned(api, env, project, goal='Fix ' + hidden)
    assert assignment['goal'] == 'Fix Approveevil[31m'
    response = await request(api, env, caller, 'assign', goal='Docs', task_type='docs', requirement_ids=[],
                             acceptance=['Line⁦ ok⁩'], need={'role': 'general', 'classification_reason': 'Docs'})
    assert response['ok'] and response['result']['acceptance'] == ['Line ok'], response
    refused = await request(api, env, caller, 'assign', goal='​‮', task_type='docs', requirement_ids=[],
                            acceptance=[], need={'role': 'general', 'classification_reason': 'Docs'})
    assert not refused['ok'] and 'visible text' in refused['error']
    fields = delivery.report_fields({'status': 'done', 'summary': 'Done', 'findings_md': '# Finding\n\t' + hidden})
    assert fields['findings_md'] == '# Finding\n\tApproveevil[31m'


async def test_a_later_report_never_moves_a_verified_or_integrated_assignment_back(runtime, api, env, project):
    from test_team import request

    from acc.server.orchestrator import assignments
    caller, assignment = await assigned(api, env, project)
    selected = (await request(api, env, caller, 'select', assignment=assignment['id']))['result']
    helper = (await request(api, env, caller, 'start', assignment=assignment['id'], selection=selected['id'], name='Worker'))['result']
    for state in ('verified', 'integrated'):
        with api.app.state.db() as db:
            db.get(assignments.Assignment, assignment['id']).state = state
            db.commit()
        for status in ('partial', 'blocked', 'done'):
            assert (await request(api, env, helper, 'report', status=status, summary=f'After {state}'))['ok']
            with api.app.state.db() as db:
                assert db.get(assignments.Assignment, assignment['id']).state == state


def test_integration_takes_verified_at_the_verified_head_when_verification_exists():
    from types import SimpleNamespace

    from acc.server.orchestrator.assignments import settle
    head, newer = 'a' * 40, 'b' * 40
    latest = SimpleNamespace(merged=True)
    def run(state, check, *, report_head=head, verified_head=None, included=(head, newer)):
        row = SimpleNamespace(state=state, verified_head=verified_head)
        settle(row, latest, SimpleNamespace(status='done', head=report_head), set(included), check)
        return row.state, row.verified_head
    # No verification for this work (gate queue off, no review): today's rule.
    assert run('reported', None) == ('integrated', None)
    # Verification exists and passes at the merged head: verified, then integrated, never skipped.
    assert run('reported', {'verified': True, 'head': head}) == ('integrated', head)
    # Verification exists but blocks, or verified another head: not integrated.
    assert run('reported', {'verified': False, 'head': head}) == ('reported', None)
    assert run('reported', {'verified': True, 'head': newer}) == ('reported', None)
    # Already verified at an older head; new commits were merged: stays verified, not integrated.
    assert run('verified', None, report_head=newer, verified_head=head) == ('verified', head)
    assert run('verified', None, verified_head=head) == ('integrated', head)
    # A head the merge didn't include never integrates.
    assert run('reported', None, included=(newer,)) == ('reported', None)



async def test_a_change_notice_goes_through_the_vault_redactor(runtime, api, env, project, monkeypatch):
    from helpers import write_db
    from sqlalchemy import select
    from test_team import request

    from acc.server.orchestrator import assignments, delivery
    from acc.server.vault import Redactor
    caller, assignment = await assigned(api, env, project)
    selected = (await request(api, env, caller, 'select', assignment=assignment['id']))['result']
    helper = (await request(api, env, caller, 'start', assignment=assignment['id'], selection=selected['id'], name='Worker'))['result']
    sup = api.app.state.supervisor
    monkeypatch.setattr(sup.vault, 'redactor', lambda *a: Redactor([('SECRET_CANARY_VALUE_123456789', 'demo')]))
    with write_db(api.app.state.db) as db:
        assignments.invalidate(db, caller['id'], notice='Use SECRET_CANARY_VALUE_123456789 now', version=3, supervisor=sup)
        notice = db.scalar(select(delivery.MessageDelivery).where(delivery.MessageDelivery.attempt_id == helper['id']))
        assert 'SECRET_CANARY' not in notice.body and '«key: demo»' in notice.body
        with pytest.raises(LaunchRefused, match='vault redactor'):
            delivery.enqueue_in_transaction(db, None, db.get(assignments.Session, helper['id']), 'supervisor', 'x')
        db.rollback()



async def test_a_failed_start_releases_its_count_and_the_assignment_can_start_again(runtime, api, env, project, monkeypatch):
    from sqlalchemy import select
    from test_team import request

    from acc.server.models import AuditLog, Team
    from acc.server.orchestrator.assignments import Attempt
    caller, assignment = await assigned(api, env, project)
    supervisor = api.app.state.supervisor
    real = supervisor.create_session

    async def refused(*args, **kwargs):
        raise RuntimeError('synthetic launch refusal')
    monkeypatch.setattr(supervisor, 'create_session', refused)
    picked = (await request(api, env, caller, 'select', assignment=assignment['id']))['result']
    assert not (await request(api, env, caller, 'start', assignment=assignment['id'], selection=picked['id'], name='First'))['ok']
    with supervisor.db() as db:
        attempt = db.scalar(select(Attempt).where(Attempt.assignment_id == assignment['id']))
        assert attempt.state == 'failed' and db.get(Team, caller['id']).started == 0
        assert db.scalar(select(AuditLog).where(AuditLog.action == 'assignment.attempt_failed')).detail['released_start']
    monkeypatch.setattr(supervisor, 'create_session', real)
    picked = (await request(api, env, caller, 'select', assignment=assignment['id']))['result']
    again = await request(api, env, caller, 'start', assignment=assignment['id'], selection=picked['id'], name='Second')
    assert again['ok'], again
    with supervisor.db() as db:
        assert db.get(Team, caller['id']).started == 1


async def test_recovery_after_a_crash_adopts_a_created_session_or_fails_the_attempt(runtime, api, env, project):
    from datetime import timedelta

    from helpers import write_db
    from test_team import request

    from acc.server.models import Team, utcnow
    from acc.server.orchestrator import assignments
    caller, assignment = await assigned(api, env, project)
    picked = (await request(api, env, caller, 'select', assignment=assignment['id']))['result']
    helper = (await request(api, env, caller, 'start', assignment=assignment['id'], selection=picked['id'], name='Ran'))['result']
    supervisor = api.app.state.supervisor
    with write_db(api.app.state.db) as db:
        ran = db.get(assignments.Attempt, helper['id'])
        ran.state, ran.created_at = 'launching', utcnow() - timedelta(minutes=5)  # as if accd died after the commit
        row = db.get(assignments.Assignment, assignment['id'])
        lost = assignments.Attempt(id='c' * 32, assignment_id=row.id, sequence=99, state='launching', brief={},
                                   created_at=utcnow() - timedelta(minutes=5))
        fresh = assignments.Attempt(id='d' * 32, assignment_id=row.id, sequence=100, state='launching', brief={})
        db.add_all([lost, fresh])
        db.get(Team, caller['id']).started += 1  # the lost start's reserved count
        db.commit()
    await assignments.recover_launching(supervisor)
    with supervisor.db() as db:
        assert db.get(assignments.Attempt, helper['id']).state == 'running'      # its session exists: adopted
        assert db.get(assignments.Attempt, 'c' * 32).state == 'failed'           # no session: failed, count back
        assert db.get(assignments.Attempt, 'd' * 32).state == 'launching'        # too recent: may still be starting
        assert db.get(Team, caller['id']).started == 1


async def test_the_person_marks_an_inspected_attempt_failed_but_never_a_running_one(runtime, api, env, project):
    from helpers import write_db
    from test_team import request

    from acc.server.models import Team
    from acc.server.orchestrator import assignments
    caller, assignment = await assigned(api, env, project)
    picked = (await request(api, env, caller, 'select', assignment=assignment['id']))['result']
    helper = (await request(api, env, caller, 'start', assignment=assignment['id'], selection=picked['id'], name='Ran'))['result']
    url = f"/api/teams/{caller['id']}/attempts/{{}}/fail"
    refused = await api.post(url.format(helper['id']), json={'reason': 'Looks stuck'})
    assert refused.status_code == 409 and 'stop it' in refused.text
    assert (await api.post(url.format(helper['id']), json={'reason': ' '})).status_code == 422
    with write_db(api.app.state.db) as db:
        db.add(assignments.Attempt(id='e' * 32, assignment_id=assignment['id'], sequence=99, state='launching', brief={}))
        db.get(Team, caller['id']).started += 1
        db.commit()
    assert (await api.post(url.format('f' * 32), json={'reason': 'Checked'})).status_code == 404
    marked = await api.post(url.format('e' * 32), json={'reason': 'No tmux session, checkout clean'})
    assert marked.status_code == 200, marked.text
    with api.app.state.db() as db:
        assert db.get(assignments.Attempt, 'e' * 32).state == 'failed' and db.get(Team, caller['id']).started == 1



async def test_a_replaced_or_failed_attempt_stays_closed_after_a_person_resumes_its_helper(runtime, api, env, project):
    """Break-it 14: the person stopped a helper, the lead replaced it, then the person resumed the old one. Its report
    moved the assignment to reported while the replacement still ran."""
    from test_team import request

    from acc.server.models import Session
    from acc.server.orchestrator.assignments import Assignment, Attempt

    def running_again(helper):  # revived another way than the person's resume, e.g. automatic recovery
        with api.app.state.db() as db:
            db.get(Session, helper['id']).desired_state = 'running'
            db.commit()
    caller, assignment = await assigned(api, env, project)
    picked = (await request(api, env, caller, 'select', assignment=assignment['id']))['result']
    first = (await request(api, env, caller, 'start', assignment=assignment['id'], selection=picked['id'], name='First'))['result']
    assert (await api.post(f"/api/sessions/{first['id']}/stop")).status_code == 200
    # A stopped attempt that still owns its assignment runs again when the person resumes it, and reports as usual.
    resumed = await api.post(f"/api/sessions/{first['id']}/resume")
    assert resumed.status_code == 200, resumed.text
    with api.app.state.db() as db:
        assert db.get(Attempt, first['id']).state == 'running'
        assert db.get(Assignment, assignment['id']).state == 'running'
    assert (await request(api, env, first, 'report', status='partial', summary='Resumed and continuing'))['ok']
    assert (await api.post(f"/api/sessions/{first['id']}/stop")).status_code == 200
    picked = (await request(api, env, caller, 'select', assignment=assignment['id']))['result']
    second = await request(api, env, caller, 'start', assignment=assignment['id'], selection=picked['id'], name='Second')
    assert second['ok'], second
    refused = await api.post(f"/api/sessions/{first['id']}/resume")
    assert refused.status_code == 409 and 'newer attempt replaced' in refused.text, refused.text
    running_again(first)
    late = await request(api, env, first, 'report', status='done', summary='Old helper says done')
    assert not late['ok'] and 'newer attempt replaced this one' in late['error'], late
    with api.app.state.db() as db:
        assert db.get(Attempt, first['id']).state == 'cancelled'
        assert db.get(Assignment, assignment['id']).state == 'running'
    # The person inspected the replacement and marked it failed: it stays failed.
    helper = second['result']
    assert (await api.post(f"/api/sessions/{helper['id']}/stop")).status_code == 200
    with api.app.state.db() as db:
        db.get(Attempt, helper['id']).state = 'blocked'  # as a blocked report left it, before the person's check
        db.commit()
    assert (await api.post(f"/api/teams/{caller['id']}/attempts/{helper['id']}/fail", json={'reason': 'Checked: broken'})).status_code == 200
    refused = await api.post(f"/api/sessions/{helper['id']}/resume")
    assert refused.status_code == 409 and 'marked failed' in refused.text, refused.text
    running_again(helper)
    late = await request(api, env, helper, 'report', status='done', summary='Failed helper says done')
    assert not late['ok'] and 'marked failed' in late['error'], late


async def test_image_selection_refusal_has_deduplicated_check_action(runtime, api, env, project):
    from test_team import request
    caller, assignment = await assigned(api, env, project, requires=['image_input'])
    for _ in range(2):
        result = await request(api, env, caller, 'select', assignment=assignment['id'])
        assert result['ok'] and result['result']['chosen'] is None
    cards = [i for i in (await api.get('/api/attention')).json() if i['kind'] == 'image_check']
    assert len(cards) == 1
    assert cards[0]['image_check']['project_id'] == project['id']
