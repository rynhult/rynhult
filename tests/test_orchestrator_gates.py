"""Orchestrator v2 slice 5: gate queue with slots and host budget, number reservations and restacking.

Real git repositories and real processes on the broker side; the end-to-end tests drive a real lead and helper through
their spools (test_team) and the gate runs a real (tiny) test command that writes a JUnit report."""
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest
from helpers import wait_for, write_db
from isolated_settings import isolated_broker
from sqlalchemy import select
from test_changes import git, repo
from test_orchestrator_selection_api import assigned_start
from test_team import lead, request

from acc.broker import gate_ops
from acc.broker.ops import OpError
from acc.server.models import AuditLog, Project, Session, Setting, Team, utcnow
from acc.server.orchestrator import gates, reservations
from acc.server.supervisor_common import LaunchRefused

# A test command: writes a JUnit report with one case per argument (name=pass|fail), exit 1 on any failure.
JUNIT = r'''
import sys
report, cases = sys.argv[1], sys.argv[2:] or ['tests/test_demo.py::test_ok=pass']
rows, failed = [], 0
for case in cases:
    test_id, outcome = case.rsplit('=', 1)
    path, name = test_id.split('::', 1)
    cls = path[:-3].replace('/', '.')
    body = '<failure message="x"/>' if outcome == 'fail' else ''
    failed += outcome == 'fail'
    rows.append(f'<testcase classname="{cls}" name="{name}">{body}</testcase>')
open(report, 'w').write('<testsuites><testsuite>' + ''.join(rows) + '</testsuite></testsuites>')
sys.exit(1 if failed else 0)
'''


@pytest.fixture(autouse=True)
def preview(monkeypatch):
    """The orchestrator v2 preview on (ACC_ORCHESTRATOR=1), before the app is built: without it, no v2 route exists."""
    monkeypatch.setenv('ACC_ORCHESTRATOR', '1')


def config(tmp_path):
    return isolated_broker(tmp_path, socket_path=tmp_path / 's', run_dir=tmp_path / 'run', tmux_socket='x', allowed_roots=(tmp_path,),
                        allowed_uids=frozenset(), providers={}, hook_path='x')


def commit(path, name, text='x', message=None):
    (path / name).parent.mkdir(parents=True, exist_ok=True)
    (path / name).write_text(text)
    git(path, 'add', '.')
    git(path, 'commit', '-qm', message or f'add {name}')
    return git_out(path, 'rev-parse', 'HEAD')


def identity(path):
    """A restack commits as the agent account, which has a git identity (as test_team's merge test sets)."""
    git(path, 'config', 'user.name', 'Taylor')
    git(path, 'config', 'user.email', 'taylor@example.com')


def git_out(path, *args):
    return subprocess.run(['git', '-C', str(path), *args], capture_output=True, text=True, check=True).stdout.strip()


# ---- settings and validation ------------------------------------------------------------------------------------

def test_owner_defaults_are_off_with_two_slots_one_full_suite_and_ceiling_16():
    assert gates.DEFAULT_BUDGET == {'enabled': False, 'gate_slots': 2, 'full_suites_max': 1, 'worker_tests': 'related',
                                    'load_ceiling': 16.0, 'job_minutes': 120, 'log_mib': 50, 'memory_gib': 8}
    assert gates.check_budget({'enabled': True})['gate_slots'] == 2
    for bad in [{'gate_slots': 0}, {'gate_slots': 9}, {'full_suites_max': 3}, {'load_ceiling': 0}, {'load_ceiling': True},
                {'enabled': 'yes'}, {'worker_tests': 'all'}, {'surprise': 1}, [], {'job_minutes': 4}, {'log_mib': 0},
                {'memory_gib': 0.5}, {'job_minutes': True}]:
        with pytest.raises(ValueError):
            gates.check_budget(bad)


@pytest.mark.parametrize('commands', [
    {'full': ['pytest']},                                                          # no related
    {'full': ['pytest', '--junitxml={junit}'], 'related': ['pytest']},             # no report
    {'full': ['-rf', '{junit}'], 'related': ['pytest', '{junit}']},                # option as the program
    {'full': ['pytest', '{junit}', '{tests}'], 'related': ['pytest', '{junit}']},  # {tests} outside rerun
    {'full': ['pytest', '{junit}'], 'related': ['pytest', '{junit}'], 'rerun': ['pytest', '{junit}']},  # rerun without {tests}
    {'full': ['pytest', '{junit}\n'], 'related': ['pytest', '{junit}']},           # control character
    {'full': ['pytest', '{junit}'], 'related': ['pytest', '{junit}'], 'shell': ['sh']},
])
def test_gate_commands_are_owner_argv_with_fixed_placeholders_only(commands):
    with pytest.raises(ValueError):
        gates.check_commands(commands)


# ---- broker: numbers, preparation, processes, restack ------------------------------------------------------------

def test_prepare_refuses_a_branch_not_on_the_current_base_and_reports_added_numbers(tmp_path):
    cfg, ws = config(tmp_path), tmp_path / 'ws'
    repo(ws)
    commit(ws, 'src/acc/server/migrations/versions/0054_oidc.py')
    git(ws, 'checkout', '-qb', 'acc/team-one')
    commit(ws, 'src/acc/server/migrations/versions/0062_gates.py')
    commit(ws, 'docs/decisions/0070-gates.md')
    git(ws, 'checkout', '-q', 'main')
    job = 'a' * 32
    ready = gate_ops.gate_prepare(cfg, str(ws), job, 'acc/team-one', 'main')
    assert ready['based'] and ready['added'] == {'migration': [62], 'decision': [70]}
    assert ready['base_numbers'] == {'migration': [54], 'decision': []}
    assert (cfg.run_dir / f'gate-{job}' / 'checkout' / 'docs/decisions/0070-gates.md').exists()
    assert gate_ops.gate_numbers(cfg, str(ws)) == {'base': {'migration': [54], 'decision': []},
                                                    'pending': {'migration': [54, 62], 'decision': [70]}}
    gate_ops.gate_cleanup(cfg, job)
    assert not (cfg.run_dir / f'gate-{job}' / 'checkout').exists()
    commit(ws, 'later.txt')  # main moved: the branch must be restacked before it is gated
    stale = gate_ops.gate_prepare(cfg, str(ws), job, 'acc/team-one', 'main')
    assert not stale['based'] and not (cfg.run_dir / f'gate-{job}' / 'checkout').exists()
    for bad in [('../x', 'main'), ('acc/team-one', 'origin/main'), ('main', 'main')]:
        with pytest.raises(OpError):
            gate_ops.gate_prepare(cfg, str(ws), job, *bad)


def test_a_gate_process_reports_from_its_record_and_stops_only_by_it(tmp_path):
    cfg, ws, job = config(tmp_path), tmp_path / 'ws', 'b' * 32
    repo(ws)
    commit(ws, 'tests/test_demo.py')
    git(ws, 'checkout', '-qb', 'acc/team-two')
    gate_ops.gate_prepare(cfg, str(ws), job, 'acc/team-two', 'main')
    record = gate_ops.gate_start(cfg, job, [sys.executable, '-c', JUNIT, '{junit}', 'tests/test_demo.py::test_a=pass',
                                            'tests/test_demo.py::test_b[x]=fail'])
    assert record['pid'] > 1 and record['start']
    deadline = time.monotonic() + 20
    while (status := gate_ops.gate_status(cfg, job, record['pid'], record['start']))['running'] and time.monotonic() < deadline:
        time.sleep(0.1)
    assert status == {'running': False, 'exit': 1, 'counts': {'tests': 2, 'failed': 1, 'skipped': 0, 'passed': 1},
                      'failed': ['tests/test_demo.py::test_b[x]'], 'rerunnable': True, 'timed_out': False}
    # A long job: the broker restarts (its child handles are gone) and the record still finds and stops it.
    record = gate_ops.gate_start(cfg, job, [sys.executable, '-c', 'import time,sys; open(sys.argv[1],"w"); time.sleep(60)', '{junit}'])
    gate_ops._children.clear()
    assert gate_ops.gate_status(cfg, job, record['pid'], record['start'])['running']
    assert gate_ops.gate_status(cfg, job, record['pid'], 'not-its-start-time')['running'] is False  # a reused pid
    assert gate_ops.gate_stop(cfg, job, record['pid'], record['start'])['signal'] == 'SIGTERM'
    deadline = time.monotonic() + 20
    while gate_ops.gate_status(cfg, job, record['pid'], record['start'])['running'] and time.monotonic() < deadline:
        time.sleep(0.1)
    assert gate_ops.gate_status(cfg, job, record['pid'], record['start']) == {
        'running': False, 'exit': None, 'counts': {}, 'failed': [], 'rerunnable': False, 'timed_out': False}
    assert '<failure message="x"/>' in (cfg.run_dir / f'gate-{job}' / 'first-junit.xml').read_text()
    assert (cfg.run_dir / f'gate-{job}' / 'first-exit').read_text() == '1'
    gate_ops.gate_cleanup(cfg, job)


def stacked(tmp_path):
    """main <- lead branch <- helper branch, then the lead's branch is squash-merged into main."""
    ws = tmp_path / 'ws'
    repo(ws)
    identity(ws)
    git(ws, 'checkout', '-qb', 'acc/team-lead')
    commit(ws, 'lead.txt', 'lead work')
    git(ws, 'worktree', 'add', '-q', '-b', 'acc/team-helper', str(tmp_path / 'helper'))
    helper = tmp_path / 'helper'
    commit(helper, 'helper.txt', 'helper work', 'Helper work')
    git(ws, 'checkout', '-q', 'main')
    git(ws, 'merge', '-q', '--squash', 'acc/team-lead')
    git(ws, 'commit', '-qm', 'Squash-merge the lead')
    return ws, helper


def test_restack_replays_only_the_helpers_own_commits_onto_main_after_a_squash_merge(tmp_path):
    cfg = config(tmp_path)
    ws, helper = stacked(tmp_path)
    result = gate_ops.gate_restack(cfg, str(helper), 'acc/team-helper', 'acc/team-lead', 'main')
    assert result['restacked'] and result['replayed'] == 1 and result['onto'] == 'main'
    assert git_out(helper, 'log', '--format=%s', 'main..acc/team-helper') == 'Helper work'
    assert gate_ops.gate_prepare(cfg, str(ws), 'c' * 32, 'acc/team-helper', 'main')['based']


def test_restack_puts_only_the_helpers_own_patches_on_top(tmp_path):
    """Review 2026-10-25 (OV-6, threat review S5-4): when main already has the helper's change as another commit, git
    would quietly drop the helper's commit; the restack refuses instead and changes nothing."""
    cfg = config(tmp_path)
    ws, helper = stacked(tmp_path)
    commit(ws, 'helper.txt', 'helper work', 'The same change, landed on main another way')
    before = git_out(helper, 'rev-parse', 'HEAD')
    result = gate_ops.gate_restack(cfg, str(helper), 'acc/team-helper', 'acc/team-lead', 'main')
    assert not result['restacked'] and "own patches" in result['error'] and result['head'] == before
    assert git_out(helper, 'rev-parse', 'HEAD') == before and git_out(helper, 'status', '--porcelain') == ''


def test_restack_refuses_on_conflict_and_leaves_the_helper_untouched(tmp_path):
    cfg = config(tmp_path)
    ws, helper = stacked(tmp_path)
    commit(ws, 'helper.txt', 'someone else changed this on main')
    before = git_out(helper, 'rev-parse', 'HEAD')
    result = gate_ops.gate_restack(cfg, str(helper), 'acc/team-helper', 'acc/team-lead', 'main')
    assert result == {**result, 'restacked': False, 'conflicts': ['helper.txt'], 'head': before}
    assert git_out(helper, 'rev-parse', 'HEAD') == before and git_out(helper, 'status', '--porcelain') == ''
    (helper / 'dirty.txt').write_text('uncommitted')
    with pytest.raises(OpError, match='uncommitted'):
        gate_ops.gate_restack(cfg, str(helper), 'acc/team-helper', 'acc/team-lead', 'main')


# The test's own setup commands, after planting, with the planted programs off.
TEST_GIT_OFF = ('-c', 'core.fsmonitor=false', '-c', 'filter.evil.process=', '-c', 'filter.evil.clean=',
                '-c', 'filter.evil.smudge=')


def plant_repository_programs(repo_dir, tmp_path):
    """What a hostile agent can write into its own repository: an fsmonitor command, clean/smudge/process filters and
    a merge driver, all selected by attributes and each leaving a marker if git runs it. Returns the marker folder."""
    marks = tmp_path / 'marks'
    marks.mkdir()
    for kind, body in (('smudge', 'cat'), ('clean', 'cat'), ('fsmonitor', 'exit 1'), ('process', 'exit 1'), ('driver', 'exit 1')):
        script = tmp_path / f'{kind}.sh'
        script.write_text(f'#!/bin/sh\ntouch {marks}/{kind}\n{body}\n')
        script.chmod(0o755)
    common = Path(git_out(repo_dir, 'rev-parse', '--path-format=absolute', '--git-common-dir'))
    (common / 'info').mkdir(exist_ok=True)
    (common / 'info' / 'attributes').write_text('* filter=evil merge=evil\n')
    for key, value in (('core.fsmonitor', 'fsmonitor'), ('filter.evil.smudge', 'smudge'), ('filter.evil.clean', 'clean'),
                       ('filter.evil.process', 'process'), ('merge.evil.driver', 'driver')):
        git(repo_dir, 'config', key, str(tmp_path / f'{value}.sh'))
    return marks


def test_gate_heads_reads_each_branch_and_refuses_anything_else(tmp_path):
    cfg = config(tmp_path)
    ws, helper = stacked(tmp_path)
    found = gate_ops.gate_heads(cfg, str(ws), ['acc/team-helper', 'acc/team-gone'])['heads']
    assert found == {'acc/team-helper': git_out(helper, 'rev-parse', 'HEAD'), 'acc/team-gone': None}
    for branches in (['origin/main'], ['acc/x', '--all'], 'acc/x', ['acc/x'] * 101):
        with pytest.raises(OpError):
            gate_ops.gate_heads(cfg, str(ws), branches)


def test_restack_runs_no_repository_controlled_program(tmp_path):
    """Review 2026-10-25 (threat review S5-5): hooks, fsmonitor, filters and merge drivers stay off, on success and on
    a conflict alike."""
    cfg = config(tmp_path)
    ws, helper = stacked(tmp_path)
    marks = plant_repository_programs(ws, tmp_path)
    result = gate_ops.gate_restack(cfg, str(helper), 'acc/team-helper', 'acc/team-lead', 'main')
    assert result['restacked'] and result['replayed'] == 1
    assert sorted(p.name for p in marks.iterdir()) == []
    (ws / 'helper.txt').write_text('someone else changed this on main')
    git(ws, *TEST_GIT_OFF, 'add', 'helper.txt')
    git(ws, *TEST_GIT_OFF, 'commit', '-qm', 'main changes the helper file')
    result = gate_ops.gate_restack(cfg, str(helper), 'acc/team-helper', 'acc/team-lead', 'main')
    assert not result['restacked'] and result['conflicts'] == ['helper.txt']
    assert sorted(p.name for p in marks.iterdir()) == []


# ---- accd: queue, scheduling, reservations ------------------------------------------------------------------------

class FakeMachine:
    """The broker's gate methods, scripted: start order, load, results."""
    def __init__(self, load=1.0):
        self.load, self.started, self.finished, self.results, self.based, self.added = load, [], set(), {}, True, {}
        self.heads = {}  # branch -> its current head; None: the machine is away

    async def call(self, method, **params):
        job = params.get('job')
        if method == 'gate_load':
            return {'load1': self.load}
        if method == 'gate_prepare':
            return {'head': 'f' * 40, 'base_tip': 'f' * 40, 'runner_version': 'fixture/gate-1', 'based': self.based, 'added': self.added, 'base_numbers': {'migration': [54]}}
        if method == 'gate_start':
            self.started.append((job, params['argv']))
            self.limits = params.get('limits')
            return {'pid': 4242, 'start': '1', 'log': '/x/gate.log'}
        if method == 'gate_status':
            if job not in self.finished:
                return {'running': True}
            return {'running': False, **self.results.get(job, {'exit': 0, 'counts': {'tests': 1}, 'failed': []})}
        if method in ('gate_cleanup', 'gate_stop'):
            return {}
        if method == 'gate_heads':
            if self.heads is None:
                from acc.server.broker_client import BrokerUnavailable
                raise BrokerUnavailable('away')
            return {'heads': {b: self.heads.get(b, 'f' * 40) for b in params['branches']}}
        raise AssertionError(method)


@pytest.fixture
async def queue(api, project):
    from helpers import pause_supervisor
    await pause_supervisor(api)
    supervisor = api.app.state.supervisor
    machine = FakeMachine()
    supervisor.broker.call = machine.call
    with write_db(api.app.state.db) as s:
        s.merge(Setting(key=gates.BUDGET_KEY, value={**gates.DEFAULT_BUDGET, 'enabled': True}))
        s.merge(Setting(key=gates.COMMANDS_KEY, value={str(project['id']): {
            'full': ['run-full', '{junit}'], 'related': ['run-related', '{junit}'], 'rerun': ['run-again', '{junit}', '{tests}']}}))
        s.commit()
    scheduler = gates.gates(supervisor)
    scheduler.wake = lambda: None  # the test drives each tick
    return api, project, machine, scheduler


def add(api, project, branch, suite='related', priority='check', **extra):
    with write_db(api.app.state.db) as s:
        job = gates.enqueue(s, s.get(Project, project['id']), branch, suite, priority, 'casey',
                            settings=api.app.state.supervisor.settings, **extra)
        s.commit()
        return job.id


def jobs(api):
    with api.app.state.db() as s:
        return {j.id: (j.state, j.slot, j.waiting) for j in s.scalars(select(gates.GateJob))}


async def test_merge_jobs_first_two_slots_and_one_full_suite_at_a_time(queue):
    api, project, machine, scheduler = queue
    check = add(api, project, 'acc/team-a')
    full_one = add(api, project, 'acc/team-b', 'full', 'check')
    merge = add(api, project, 'acc/team-c', 'related', 'merge')
    full_two = add(api, project, 'acc/team-d', 'full', 'merge')
    await scheduler.tick()
    # merge jobs first (oldest first among them), two slots, never two full suites at once
    assert [job for job, _ in machine.started] == [merge, full_two]
    state = jobs(api)
    assert state[merge][:2] == ('running', 1) and state[full_two][:2] == ('running', 2)
    assert state[full_one] == ('queued', None, 'slot') and state[check] == ('queued', None, 'slot')
    machine.finished.add(full_two)
    await scheduler.tick()
    assert jobs(api)[full_two][0] == 'passed'
    assert [job for job, _ in machine.started][2] == check  # the freed slot goes to the oldest waiting job
    machine.finished.add(merge)
    await scheduler.tick()
    assert [job for job, _ in machine.started][3] == full_one  # no other full suite runs now
    assert machine.started[0][1] == ['run-related', '{junit}']


async def test_full_suites_wait_while_the_machine_is_over_the_load_ceiling(queue):
    api, project, machine, scheduler = queue
    machine.load = 16.5
    full = add(api, project, 'acc/team-a', 'full')
    related = add(api, project, 'acc/team-b')
    await scheduler.tick()
    assert jobs(api)[full] == ('queued', None, 'load 16.5 over 16') and jobs(api)[related][0] == 'running'
    machine.load = 9.1
    await scheduler.tick()
    assert jobs(api)[full][0] == 'running'


async def test_a_failure_reruns_only_the_failed_tests_once_then_flaky_passes(queue):
    api, project, machine, scheduler = queue
    flaky = add(api, project, 'acc/team-a')
    broken = add(api, project, 'acc/team-b')
    await scheduler.tick()
    failing = {'exit': 1, 'counts': {'tests': 3, 'failed': 1}, 'failed': ['tests/test_x.py::test_one'], 'rerunnable': True}
    machine.results = {flaky: failing, broken: failing}
    machine.finished |= {flaky, broken}
    await scheduler.tick()
    reruns = [argv for job, argv in machine.started[2:]]
    assert reruns == [['run-again', '{junit}', 'tests/test_x.py::test_one']] * 2
    assert jobs(api)[flaky][:2] == ('running', jobs(api)[flaky][1])  # same slot, attempt 2
    machine.results = {flaky: {'exit': 0, 'counts': {'tests': 1}, 'failed': []}, broken: failing}
    await scheduler.tick()
    with api.app.state.db() as s:
        passed, failed = s.get(gates.GateJob, flaky), s.get(gates.GateJob, broken)
        assert passed.state == 'retry-passed' and passed.results['flaky'] == ['tests/test_x.py::test_one']
        assert failed.state == 'failed' and failed.reason == 'failed again on the targeted rerun'


async def test_unrestacked_branches_and_unreserved_numbers_never_run(queue):
    api, project, machine, scheduler = queue
    machine.based = False
    stale = add(api, project, 'acc/team-a')
    await scheduler.tick()
    machine.based, machine.added = True, {'migration': [63]}
    with write_db(api.app.state.db) as s:  # a live team: a stopped or missing one's jobs are cancelled first
        s.add(Team(id='t' * 32, limits={}, expires_at=utcnow() + timedelta(hours=1), stopped=False, started=0))
        s.commit()
    numbered = add(api, project, 'acc/team-b', team_id='t' * 32)
    await scheduler.tick()
    with api.app.state.db() as s:
        assert s.get(gates.GateJob, stale).state == 'needs-restack'
        refused = s.get(gates.GateJob, numbered)
        assert refused.state == 'refused' and 'migration 0063 is not reserved by this team' in refused.reason
    assert machine.started == []


async def test_a_missing_branch_is_refused_but_an_unreachable_machine_only_waits(queue):
    from acc.server.broker_client import BrokerError, MachineOffline
    api, project, machine, scheduler = queue
    missing, away = add(api, project, 'acc/team-gone'), add(api, project, 'acc/team-away')
    real = machine.call

    async def call(method, **params):
        if method == 'gate_prepare':
            raise BrokerError('acc/team-gone does not exist in this repository') if params['job'] == missing else MachineOffline('pc offline')
        return await real(method, **params)
    api.app.state.supervisor.broker.call = call
    await scheduler.tick()
    with api.app.state.db() as s:
        refused, waiting = s.get(gates.GateJob, missing), s.get(gates.GateJob, away)
        assert refused.state == 'refused' and refused.reason == 'could not prepare: acc/team-gone does not exist in this repository'
        assert (waiting.state, waiting.waiting) == ('queued', 'machine unavailable')


async def test_cancel_stops_a_running_job_through_its_record(queue):
    api, project, machine, scheduler = queue
    job = add(api, project, 'acc/team-a')
    await scheduler.tick()
    response = await api.post(f'/api/gates/{job}/cancel')
    assert response.status_code == 200 and response.json()['waiting'] == 'cancel'
    machine.finished.add(job)
    await scheduler.tick()
    assert jobs(api)[job][0] == 'cancelled'


async def test_the_queue_is_off_by_default_and_its_settings_are_the_owners(api, project, monkeypatch):
    # This tests API admission, not a scheduler racing to refuse the deliberately missing branch.
    scheduler = gates.gates(api.app.state.supervisor)
    monkeypatch.setattr(scheduler, 'wake', lambda: None)
    refused = await api.post('/api/gates', json={'project': project['id'], 'branch': 'acc/team-a'})
    assert refused.status_code == 409 and 'off' in refused.json()['detail']
    assert (await api.get('/api/gates/budget')).json() == gates.DEFAULT_BUDGET
    assert (await api.put('/api/gates/budget', json={'enabled': True, 'gate_slots': 3, 'confirm_branch_code': True})).json()['gate_slots'] == 3
    assert (await api.put('/api/gates/budget', json={'gate_slots': 99})).status_code == 422
    no_commands = await api.post('/api/gates', json={'project': project['id'], 'branch': 'acc/team-a'})
    assert no_commands.status_code == 409 and 'gate commands' in no_commands.json()['detail']
    put = await api.put(f'/api/gates/projects/{project["id"]}/commands',
                        json={'full': ['pytest', '--junitxml={junit}'], 'related': ['pytest', '--junitxml={junit}', '-q']})
    assert put.status_code == 200
    queued = await api.post('/api/gates', json={'project': project['id'], 'branch': 'acc/team-a', 'priority': 'merge'})
    assert queued.status_code == 201 and queued.json()['state'] == 'queued'
    again = await api.post('/api/gates', json={'project': project['id'], 'branch': 'acc/team-a'})
    assert again.status_code == 409
    listing = (await api.get(f'/api/gates?project={project["id"]}')).json()
    assert [j['branch'] for j in listing['jobs']] == ['acc/team-a'] and listing['budget']['gate_slots'] == 3
    with api.app.state.db() as s:
        actions = [a.action for a in s.scalars(select(AuditLog).where(AuditLog.action.like('%budget%') | AuditLog.action.like('gate.%')))]
    assert actions[:3] == ['host_budget.change', 'gate.commands', 'gate.queue']  # the scheduler may already have refused the job


async def test_members_cannot_change_the_budget_commands_or_see_others_gates(api, project, env, member_accounts):
    from helpers import verified_post
    from test_auth import do_setup
    from test_users import browser, csrf, join
    await do_setup(api, env)  # the owner's browser sign-in: inviting needs a person, not the API token
    invitation = await verified_post(api, '/api/users/invites', {'role': 'member', 'account': 'dana-agent'})
    async with browser(api) as member:
        assert (await join(member, invitation.json()['link'], 'alex')).status_code == 200
        headers = csrf(member)
        assert (await member.put('/api/gates/budget', headers=headers, json={'enabled': True})).status_code == 403
        assert (await member.put(f'/api/gates/projects/{project["id"]}/commands', headers=headers,
                                 json={'full': ['x', '{junit}'], 'related': ['x', '{junit}']})).status_code == 403
        assert (await member.get(f'/api/gates?project={project["id"]}')).status_code == 404
        assert (await member.post('/api/gates', headers=headers,
                                  json={'project': project['id'], 'branch': 'acc/team-a'})).status_code == 404


async def test_reservations_skip_main_pending_branches_and_open_ones(api, project, env):
    x = await lead(api, project, env)
    with write_db(api.app.state.db) as s:
        caller = s.get(Session, x['id'])
        numbers = {'migration': {54, 55, 56, 57, 58, 59, 60, 61, 62}, 'decision': {64}}
        first = reservations.reserve(s, 'migration', caller, numbers)
        second = reservations.reserve(s, 'migration', caller, numbers)
        decision = reservations.reserve(s, 'decision', caller, numbers)
        assert (first.number, second.number, decision.number) == (63, 64, 65)
        assert reservations.check(s, caller.team_id, {'migration': {63}}, numbers) == []
        assert reservations.check(s, 'o' * 32, {'migration': {64}}, numbers) == ['migration 0064 is not reserved by this team (use reserve)']
        assert reservations.check(s, caller.team_id, {'migration': {62}}, numbers) == ['migration 0062 already exists on the base branch']
        reservations.release(s, second.id, caller)
        with pytest.raises(LaunchRefused):
            reservations.release(s, second.id, caller)
        assert reservations.reserve(s, 'migration', caller, numbers).number == 64  # the highest released number is reused
        assert reservations.reserve(s, 'migration', caller, numbers, {'migration': {70}}).number == 71  # an unmerged branch's
        assert first.state == 'reserved'  # only the main line marks a number used, never a pending branch
        reservations.mark_used(s, {'migration': {63}})
        assert first.state == 'used'
        for _ in range(reservations.OPEN_LIMIT - 3):
            reservations.reserve(s, 'decision', caller, numbers)
        with pytest.raises(LaunchRefused, match='at most 20'):
            reservations.reserve(s, 'decision', caller, numbers)
        with pytest.raises(LaunchRefused):
            reservations.reserve(s, 'port', caller, numbers)


# ---- team tools, end to end ----------------------------------------------------------------------------------------

async def team(api, project, env):
    x = await lead(api, project, env)
    identity(env['workspaces'] / 'demo')
    a = (await assigned_start(api, env, x, name='A', task='build the thing'))['result']
    with write_db(api.app.state.db) as s:
        s.merge(Setting(key=gates.BUDGET_KEY, value={**gates.DEFAULT_BUDGET, 'enabled': True}))
        s.merge(Setting(key=gates.COMMANDS_KEY, value={str(project['id']): {
            'full': [sys.executable, '-c', JUNIT, '{junit}'], 'related': [sys.executable, '-c', JUNIT, '{junit}'],
            'rerun': [sys.executable, '-c', JUNIT, '{junit}', '{tests}']}}))
        s.commit()
    return x, a


async def test_team_reserves_numbers_and_gates_its_helper_with_the_owners_command(api, project, env):
    x, a = await team(api, project, env)
    reserved = await request(api, env, x, 'reserve', kind='migration')
    assert reserved['ok'], reserved
    assert reserved['result'] == {**reserved['result'], 'state': 'reserved', 'number': 1, 'label': '0001'}
    folder = Path(a['worktree'])
    commit(folder, f"src/acc/server/migrations/versions/{reserved['result']['number']:04d}_thing.py")
    commit(folder, 'tests/test_demo.py')
    assert not (await request(api, env, a, 'gate', suite='full'))['ok']  # helpers run related tests only
    assert not (await request(api, env, a, 'gate', helper=x['id']))['ok']  # only its own subtree
    queued = await request(api, env, x, 'gate', helper=a['id'])
    assert queued['ok'] and queued['result']['base'] == x['worktree_branch']
    job = queued['result']['id']

    async def finished():
        status = await request(api, env, x, 'gate_status', job=job)
        return status['result'] if status['result']['state'] not in gates.ACTIVE else None
    result = await wait_for(finished, timeout=60)
    assert result['state'] == 'passed', result
    assert result['results']['counts']['passed'] == 1 and result['head']
    with api.app.state.db() as s:
        assert {a.action for a in s.scalars(select(AuditLog).where(AuditLog.target == job))} >= {'gate.queue', 'gate.start', 'gate.finish'}
    released = await request(api, env, x, 'release', reservation=reserved['result']['id'])
    assert released['ok'] and released['result']['state'] == 'released'


async def test_lead_restacks_a_stopped_helper_by_itself_and_the_gate_reruns_it(api, project, env):
    x, a = await team(api, project, env)
    helper, lead_tree = Path(a['worktree']), Path(x['worktree'])
    commit(helper, 'helper.txt', 'helper work', 'Helper work')
    assert not (await request(api, env, x, 'restack', helper=a['id']))['ok']  # still running
    assert (await request(api, env, x, 'stop', helper=a['id']))['ok']
    assert not (await request(api, env, a, 'restack', helper=a['id']))['ok']  # only the lead
    commit(lead_tree, 'lead.txt', 'the lead moved on')
    restacked = await request(api, env, x, 'restack', helper=a['id'])
    assert restacked['ok'] and restacked['result']['restacked'] and restacked['result']['replayed'] == 1
    assert restacked['result']['gate']['suite'] == 'related' and restacked['result']['gate']['base'] == x['worktree_branch']
    assert git_out(helper, 'log', '--format=%s', '-2') == 'Helper work\n' + git_out(lead_tree, 'log', '--format=%s', '-1')
    commit(lead_tree, 'helper.txt', 'conflicting lead change')
    before = git_out(helper, 'rev-parse', 'HEAD')
    conflict = await request(api, env, x, 'restack', helper=a['id'])
    assert conflict['ok'] and not conflict['result']['restacked'] and conflict['result']['conflicts'] == ['helper.txt']
    assert git_out(helper, 'rev-parse', 'HEAD') == before
    with api.app.state.db() as s:
        assert [a.detail['restacked'] for a in s.scalars(select(AuditLog).where(AuditLog.action == 'team.restack'))] == [True, False]


async def test_restack_and_reservations_wait_for_the_gate_queue(api, project, env):
    """Review 2026-10-26: with the gate queue off (the default), a lead can't restack a helper or hold numbers. The
    owner allowed restacking on the lead's own only because the gate then reruns the helper's tests."""
    x = await lead(api, project, env)
    identity(env['workspaces'] / 'demo')
    a = (await assigned_start(api, env, x, name='A', task='build the thing'))['result']
    helper, lead_tree = Path(a['worktree']), Path(x['worktree'])
    commit(helper, 'helper.txt', 'helper work', 'Helper work')
    assert (await request(api, env, x, 'stop', helper=a['id']))['ok']
    commit(lead_tree, 'lead.txt', 'the lead moved on')
    before = git_out(helper, 'rev-parse', 'HEAD')
    for action, args in (('restack', {'helper': a['id']}), ('reserve', {'kind': 'migration'}), ('release', {'reservation': 1})):
        refused = await request(api, env, x, action, **args)
        assert not refused['ok'] and 'gate queue is off' in refused['error'], (action, refused)
    assert git_out(helper, 'rev-parse', 'HEAD') == before
    with write_db(api.app.state.db) as s:  # on, but no gate commands for the project: nothing would rerun its tests
        s.merge(Setting(key=gates.BUDGET_KEY, value={**gates.DEFAULT_BUDGET, 'enabled': True}))
        s.commit()
    refused = await request(api, env, x, 'restack', helper=a['id'])
    assert not refused['ok'] and 'gate commands' in refused['error'], refused
    assert git_out(helper, 'rev-parse', 'HEAD') == before
    with api.app.state.db() as s:
        assert not list(s.scalars(select(AuditLog).where(AuditLog.action == 'team.restack')))


async def test_a_pass_covers_only_the_commit_it_tested(queue):
    """Review 2026-10-25 (threat review S5-3, S5-6): new commits or a restack after a pass make it stale, and an
    unreachable machine never reads as "current"."""
    api, project, machine, scheduler = queue
    job = add(api, project, 'acc/team-a')
    await scheduler.tick()
    machine.finished.add(job)
    await scheduler.tick()
    assert jobs(api)[job][0] == 'passed'

    async def shown():
        listed = (await api.get(f'/api/gates?project={project["id"]}')).json()['jobs']
        return next(j for j in listed if j['id'] == job)
    current = await shown()
    assert current['head'] == 'f' * 40 and current['stale'] is False and current['current_head'] == 'f' * 40
    machine.heads = {'acc/team-a': 'e' * 40}  # the agent committed again, or the lead restacked
    moved = await shown()
    assert moved['stale'] == 'branch moved' and moved['current_head'] == 'e' * 40
    machine.heads = None
    unknown = await shown()
    assert unknown['stale'] is None and unknown['current_head'] is None


def test_a_gate_job_is_cut_at_its_log_cap_memory_and_time_limits(tmp_path):
    """Review 2026-10-25 (OV-5, threat review S5-2): real processes, enforced by the job's own wrapper."""
    cfg = config(tmp_path)
    ws = tmp_path / 'ws'
    repo(ws)
    git(ws, 'checkout', '-qb', 'acc/team-limits')
    job = 'e' * 32
    gate_ops.gate_prepare(cfg, str(ws), job, 'acc/team-limits', 'main')
    script = ('import sys, time\n'
              'sys.stdout.write("x" * (2 << 20)); sys.stdout.flush()\n'
              'try:\n    bytearray(2 << 30); open("memory", "w").write("allowed")\n'
              'except MemoryError:\n    open("memory", "w").write("refused")\n'
              'open(sys.argv[1], "w"); time.sleep(60)\n')
    record = gate_ops.gate_start(cfg, job, [sys.executable, '-c', script, '{junit}'],
                                 limits={'seconds': 2, 'log_bytes': 1 << 20, 'memory_bytes': 1 << 30})
    deadline = time.monotonic() + 30
    while (status := gate_ops.gate_status(cfg, job, record['pid'], record['start']))['running'] and time.monotonic() < deadline:
        time.sleep(0.1)
    assert status['running'] is False and status['timed_out'] is True and status['exit'] == 124
    folder = cfg.run_dir / f'gate-{job}'
    log = (folder / 'gate.log').read_bytes()
    assert log.startswith(b'x' * 1000) and len(log) < (1 << 20) + 200 and b'gate log cut' in log
    assert (folder / 'checkout' / 'memory').read_text() == 'refused'
    with pytest.raises(OpError, match='invalid gate limits'):
        gate_ops.gate_start(cfg, job, ['true', '{junit}'], limits={'seconds': 0, 'log_bytes': 1 << 20, 'memory_bytes': 1 << 30})
    gate_ops.gate_cleanup(cfg, job)


async def test_the_host_budget_limits_reach_every_job_and_a_timeout_fails_it(queue):
    api, project, machine, scheduler = queue
    job = add(api, project, 'acc/team-slow')
    await scheduler.tick()
    assert machine.limits == {'seconds': 120 * 60, 'log_bytes': 50 << 20, 'memory_bytes': 8 << 30}
    machine.results = {job: {'exit': 124, 'timed_out': True, 'counts': {}, 'failed': ['tests/test_x.py::test_a'],
                             'rerunnable': True}}
    machine.finished.add(job)
    await scheduler.tick()
    with api.app.state.db() as s:
        row = s.get(gates.GateJob, job)
        assert row.state == 'failed' and 'time limit' in row.reason and row.attempt == 1  # never a rerun
    assert (await api.put('/api/gates/budget', json={'job_minutes': 1})).status_code == 422


async def test_gate_jobs_are_counted_per_team_and_project(queue):
    """Review 2026-10-25 (OV-7, threat review X-2, X-8, S5-9): whoever asks."""
    api, project, machine, scheduler = queue
    for n in range(gates.TEAM_ACTIVE):
        add(api, project, f'acc/team-q{n}', team_id='t' * 32)
    with pytest.raises(LaunchRefused, match='this team already has'):
        add(api, project, 'acc/team-q-more', team_id='t' * 32)
    add(api, project, 'acc/team-full-one', 'full', team_id='u' * 32)
    with pytest.raises(LaunchRefused, match='full suites'):
        add(api, project, 'acc/team-full-two', 'full', team_id='u' * 32)


async def test_a_stopped_or_expired_teams_queued_gates_never_start(queue):
    """A team's gate runs its branch's code as the project account; once the team is stopped or expired it no longer
    has that authority, so its jobs still waiting are cancelled instead of started."""
    api, project, machine, scheduler = queue
    with write_db(api.app.state.db) as s:
        s.add(Team(id='t' * 32, limits={}, expires_at=utcnow() + timedelta(hours=1), stopped=True, started=0))
        s.add(Team(id='e' * 32, limits={}, expires_at=utcnow() - timedelta(minutes=1), stopped=False, started=0))
        s.commit()
    stopped = add(api, project, 'acc/team-stopped', team_id='t' * 32)
    expired = add(api, project, 'acc/team-expired', team_id='e' * 32)
    own = add(api, project, 'acc/owner-branch')
    await scheduler.tick()
    await scheduler.tick()  # the cancelled jobs held this tick's two slots
    assert [job for job, _ in machine.started] == [own]
    state = jobs(api)
    assert state[stopped][0] == 'cancelled' and state[expired][0] == 'cancelled'


# ---- gates trust branch code (review L-1) ---------------------------------------------------------------------------

async def test_turning_the_queue_on_needs_the_owner_to_accept_that_gates_trust_branch_code(api):
    from acc.server.models import AuditLog
    refused = await api.put('/api/gates/budget', json={'enabled': True})
    assert refused.status_code == 409 and refused.json()['detail'] == gates.TRUST_WARNING
    assert 'outside the helpers' in gates.TRUST_WARNING
    assert (await api.get('/api/gates/budget')).json()['enabled'] is False  # nothing saved
    assert (await api.put('/api/gates/budget', json={'enabled': True, 'confirm_branch_code': True})).json()['enabled'] is True
    # Already on: other changes don't ask again; turning it off never asks.
    assert (await api.put('/api/gates/budget', json={'enabled': True, 'gate_slots': 3})).status_code == 200
    assert (await api.put('/api/gates/budget', json={'enabled': False})).status_code == 200
    assert (await api.put('/api/gates/budget', json={'enabled': True})).status_code == 409  # each turn-on asks
    assert (await api.put('/api/gates/budget', json={'enabled': True, 'confirm_branch_code': 'yes'})).status_code == 409
    with api.app.state.db() as s:
        changes = [r.detail for r in s.scalars(select(AuditLog).where(AuditLog.action == 'host_budget.change'))]
    assert [c.get('branch_code_trusted') for c in changes] == [True, None, None]


async def test_a_container_project_never_gates_its_branch_code_on_the_host(queue, monkeypatch):
    import dataclasses
    api, project, machine, scheduler = queue
    supervisor = api.app.state.supervisor
    queued = add(api, project, 'acc/team-native')  # queued while the project's helpers still ran on the host
    with write_db(api.app.state.db) as s:
        s.get(Project, project['id']).containers = True
        s.commit()
    boxed = dataclasses.replace(supervisor.settings, containers_enabled=True)
    monkeypatch.setattr(supervisor, 'settings', boxed)
    monkeypatch.setattr(api.app.state.ctx, 'settings', boxed)  # the API reads the same settings through its context
    with pytest.raises(gates.LaunchRefused, match='run in containers'):
        add(api, project, 'acc/team-boxed')
    refused = await api.post('/api/gates', json={'project': project['id'], 'branch': 'acc/team-boxed'})
    assert refused.status_code == 409 and 'outside the sandbox' in refused.json()['detail']
    # A job queued before the project switched to containers is refused at start, never run on the host.
    await scheduler.tick()
    assert jobs(api)[queued][0] == 'refused' and machine.started == []
    with api.app.state.db() as s:
        assert gates.CONTAINER_REFUSAL in s.get(gates.GateJob, queued).reason


def test_gate_checkout_uses_the_merged_git_hardening(tmp_path):
    cfg = config(tmp_path)
    ws = tmp_path / 'repo'
    repo(ws)
    git(ws, 'checkout', '-qb', 'acc/gate-checkout')
    commit(ws, 'work.txt', 'gate input')
    marks = plant_repository_programs(ws, tmp_path)
    job = 'd' * 32
    try:
        result = gate_ops.gate_prepare(cfg, str(ws), job, 'acc/gate-checkout', 'main')
        assert result['based']
        assert list(marks.iterdir()) == []
    finally:
        gate_ops.gate_cleanup(cfg, job)
