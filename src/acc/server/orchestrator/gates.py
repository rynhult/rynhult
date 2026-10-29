"""Orchestrator v2, slice 5: gate queue with slots, the host test budget and restacking (gaps 5, 6, 10 and 11 of
docs/ORCHESTRATOR-V2.md).

Off by default: nothing queues or runs until an owner turns the host budget on. A gate job runs one fixed command the
owner set for the project (argv, never an agent's text) in a fresh detached worktree, as the project's agent account,
through that machine's broker (broker/gate_ops.py). accd records the process identity the broker returns and stops a
job only by that record, never by matching process names.

Scheduling: merge jobs before check jobs, then oldest first; at most `gate_slots` jobs run, at most `full_suites_max`
of them full suites, and a full suite waits while the machine's 1-minute load is over `load_ceiling`. A branch that
isn't based on its base's current tip is refused as needing a restack. Numbers a branch adds must be reserved
(reservations.py). A failure reruns only the failed tests once in the same slot; a new pass then is `retry-passed`; only owner-listed tests become `known-flake`.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import uuid
from datetime import datetime

from sqlalchemy import JSON, ForeignKey, Integer, String, select
from sqlalchemy.orm import Mapped, mapped_column

from ..broker_client import BrokerError, BrokerUnavailable
from ..models import AuditLog, Base, Project, Session, Setting, Team, UTCDateTime, utcnow
from ..supervisor_common import LaunchRefused
from . import reservations
from .switch import enabled as orchestrator_enabled

log = logging.getLogger(__name__)

BUDGET_KEY = 'orchestrator.host_budget'
COMMANDS_KEY = 'orchestrator.gate_commands'
FLAKES_KEY = 'orchestrator.known_flakes'
# The owner's defaults for this host (design section 10, question 3); `enabled` keeps the queue off until turned on.
DEFAULT_BUDGET = {'enabled': False, 'gate_slots': 2, 'full_suites_max': 1, 'worker_tests': 'related', 'load_ceiling': 16.0,
                  # Per job (security review 2026-10-25, OV-5; threat review S5-2): stopped after this many minutes,
                  # its log cut at this many MiB, each of its processes limited to this much memory (GiB).
                  'job_minutes': 120, 'log_mib': 50, 'memory_gib': 8}
BRANCH = re.compile(r'^acc/[a-z0-9][a-z0-9-]{0,80}(?:/work)?$')
SUITES = ('full', 'related')
PRIORITIES = ('merge', 'check')
ACTIVE = ('queued', 'running')
DONE = ('passed', 'failed', 'retry-passed', 'known-flake', 'cancelled', 'needs-restack', 'refused')
PASSED = ('passed', 'known-flake')  # a result for the job's `head` only, never for whatever the branch is now
TICK = 5.0
RERUN_LIMIT = 50  # more failures than this are not flakes: no targeted rerun
TEAM_ACTIVE, PROJECT_ACTIVE = 10, 50  # queued or running gate jobs (threat review X-8)
# Gates trust branch code (review L-1): a job runs the owner's commands on the branch's files as the project's account,
# on the machine, with its network: outside a helper's container, permission mode and guardrails. The owner confirms
# that when turning the queue on. A project whose helpers run in containers never has its branch code run that way:
# until a gate can run inside the helpers' container (a root-helper operation still to build and qualify), its jobs
# are refused.
TRUST_WARNING = ("Gate jobs run the gate commands on a branch's files as the project's account, on this machine, with "
                 "its network. That is outside the helpers' sandbox, permission mode and guardrails: a helper's branch "
                 "can change what a test run does. Turn the gate queue on only for projects whose helpers you trust "
                 "with that account.")
CONTAINER_REFUSAL = ("this project's helpers run in containers, and a gate can't run inside one yet, so Rynhult won't run "
                     "its branch code outside the sandbox; test it in a helper's container instead")


def sandboxed(project: Project, settings) -> bool:
    """Whether the project's helpers run in containers: then its branch code never runs in a gate on the host.

    The project's own choice decides, not whether ACC_CONTAINERS is on now: with containers off, its sessions are
    refused rather than run natively, so its branches still hold code only a container ran (v2 gate sandbox review,
    GS-1). `settings` stays for callers and for a later in-container gate."""
    return bool(project.containers)


class GateJob(Base):
    __tablename__ = 'gate_job'
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey('project.id', ondelete='CASCADE'), index=True)
    team_id: Mapped[str | None] = mapped_column(String(32), index=True)
    branch: Mapped[str] = mapped_column(String(100))
    base: Mapped[str] = mapped_column(String(100), default='main')
    stack_parent: Mapped[str | None] = mapped_column(String(32))
    head: Mapped[str | None] = mapped_column(String(64))
    base_sha: Mapped[str | None] = mapped_column(String(64))
    commands_digest: Mapped[str | None] = mapped_column(String(64))
    runner_version: Mapped[str | None] = mapped_column(String(80))
    suite: Mapped[str] = mapped_column(String(16))                 # full | related
    priority: Mapped[str] = mapped_column(String(8))               # merge | check
    state: Mapped[str] = mapped_column(String(16), default='queued', index=True)
    waiting: Mapped[str | None] = mapped_column(String(32))        # why a queued job hasn't started (load, full suites)
    slot: Mapped[int | None] = mapped_column(Integer)
    attempt: Mapped[int] = mapped_column(Integer, default=0)       # 1: the suite; 2: the targeted rerun
    host: Mapped[str] = mapped_column(String(64), default='local')
    account: Mapped[str] = mapped_column(String(32))
    pid_record: Mapped[dict | None] = mapped_column(JSON)          # {machine, pid, start}: what the broker started
    log_path: Mapped[str | None] = mapped_column(String(300))
    requested_by: Mapped[str] = mapped_column(String(80))
    reason: Mapped[str | None] = mapped_column(String(500))
    results: Mapped[dict] = mapped_column(JSON, default=dict)      # {exit, counts, failed[], flaky[]}
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


# ---- settings ---------------------------------------------------------------------------------------------------

def budget(db) -> dict:
    row = db.get(Setting, BUDGET_KEY)
    return {**DEFAULT_BUDGET, **(row.value if row else {})}


def check_budget(value) -> dict:
    if not isinstance(value, dict) or set(value) - set(DEFAULT_BUDGET):
        raise ValueError('unknown host budget fields')
    merged = {**DEFAULT_BUDGET, **value}
    slots, full, ceiling = merged['gate_slots'], merged['full_suites_max'], merged['load_ceiling']
    if (type(merged['enabled']) is not bool or type(slots) is not int or not 1 <= slots <= 8
            or type(full) is not int or not 1 <= full <= slots or merged['worker_tests'] not in ('related', 'full')
            or isinstance(ceiling, bool) or not isinstance(ceiling, (int, float)) or not 0.5 <= ceiling <= 1024):
        raise ValueError('gate slots 1-8, full suites 1 to the slot count, load ceiling 0.5-1024')
    for key, low, high in (('job_minutes', 5, 1440), ('log_mib', 1, 1024), ('memory_gib', 1, 256)):
        if type(merged[key]) is not int or not low <= merged[key] <= high:
            raise ValueError('job time limit 5-1440 minutes, log cap 1-1024 MiB, memory per process 1-256 GiB')
    return {**merged, 'load_ceiling': float(ceiling)}


ARG = re.compile(r'^[^\x00-\x1f]{1,512}$')


def check_commands(value) -> dict:
    """Per project: {full: argv, related: argv, rerun?: argv}. Owner-set argv lists, run without a shell. Placeholders:
    {junit} (the report file the gate reads) anywhere in an argument; {tests} as a whole argument (rerun only)."""
    if not isinstance(value, dict) or not {'full', 'related'} <= set(value) or set(value) - {'full', 'related', 'rerun'}:
        raise ValueError('commands need full and related (and optionally rerun) argv lists')
    for suite, argv in value.items():
        if (not isinstance(argv, list) or not 1 <= len(argv) <= 64 or not all(isinstance(a, str) and ARG.fullmatch(a) for a in argv)
                or argv[0].startswith('-') or '{junit}' not in ''.join(argv)):
            raise ValueError(f'{suite}: 1-64 arguments without control characters, writing a JUnit report to {{junit}}')
        if ('{tests}' in argv) != (suite == 'rerun'):
            raise ValueError('only the rerun command takes {tests}, as a whole argument')
    return value


def commands(db, project_id: int) -> dict | None:
    row = db.get(Setting, COMMANDS_KEY)
    return (row.value if row else {}).get(str(project_id))


def command_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def flakes(db, project_id):
    row = db.get(Setting, FLAKES_KEY)
    return (row.value if row else {}).get(str(project_id), [])


def stale_reason(job, heads, current_commands):
    # Missing legacy evidence or an offline machine is unknown, never a fresh pass.
    if job.commands_digest != command_digest(current_commands):
        return 'commands changed'
    if job.branch not in heads or job.base not in heads:
        return None
    if heads[job.branch] != job.head:
        return 'branch moved'
    if heads[job.base] != job.base_sha:
        return 'base moved'
    if not job.runner_version:
        return None
    return False


def integration_pass(db, job):
    if job is None:
        return False
    if job.state in PASSED:
        return True
    accepted = (job.results or {}).get('accepted_flake')
    if job.state != 'retry-passed' or not isinstance(accepted, dict):
        return False
    from .worklist import WorkItem
    item = db.get(WorkItem, accepted.get('item'))
    return bool(item and item.team_id == job.team_id and item.kind == 'task' and item.state != 'dismissed')


def cancel_team(db, team_id, actor):
    for job in db.scalars(select(GateJob).where(GateJob.team_id == team_id, GateJob.state.in_(ACTIVE))):
        cancel(db, job, actor, 'cancelled: team stopped')


# ---- queue ------------------------------------------------------------------------------------------------------

def enqueue(db, project: Project, branch: str, suite: str, priority: str, requested_by: str, *, settings, team_id=None,
            base='main', stack_parent=None) -> GateJob:
    config = budget(db)
    if not orchestrator_enabled():
        raise LaunchRefused('Orchestrator v2 is off')
    if not config['enabled']:
        raise LaunchRefused('the gate queue is off; an owner turns it on in the host budget')
    if project.deleting:
        raise LaunchRefused('this project is being deleted; no new gates can start')
    if not isinstance(branch, str) or not BRANCH.fullmatch(branch):
        raise LaunchRefused('gates run Rynhult topic branches (acc/...)')
    if base != 'main' and not BRANCH.fullmatch(base):
        raise LaunchRefused('a gate base is main or a Rynhult topic branch')
    if suite not in SUITES or priority not in PRIORITIES:
        raise LaunchRefused('suite is full or related; priority is merge or check')
    if commands(db, project.id) is None:
        raise LaunchRefused('an owner has not set gate commands for this project')
    if sandboxed(project, settings):
        raise LaunchRefused(CONTAINER_REFUSAL)
    if db.scalar(select(GateJob.id).where(GateJob.project_id == project.id, GateJob.branch == branch,
                                          GateJob.suite == suite, GateJob.state.in_(ACTIVE))):
        raise LaunchRefused('this branch already has that suite queued or running')
    # Counted per team and per project, whoever asks (security review 2026-10-25, OV-7; threat review X-2, X-8, S5-9):
    # "full suites only from the lead" is a convention within one account, these counts are the limit.
    def active(*where):
        return len(list(db.scalars(select(GateJob.id).where(GateJob.state.in_(ACTIVE), *where).limit(PROJECT_ACTIVE + 1))))
    if active(GateJob.project_id == project.id) >= PROJECT_ACTIVE:
        raise LaunchRefused(f'this project already has {PROJECT_ACTIVE} gate jobs queued or running')
    if team_id is not None:
        if active(GateJob.team_id == team_id) >= TEAM_ACTIVE:
            raise LaunchRefused(f'this team already has {TEAM_ACTIVE} gate jobs queued or running')
        if suite == 'full' and active(GateJob.team_id == team_id, GateJob.suite == 'full') >= config['full_suites_max']:
            raise LaunchRefused('this team already has its full suites queued or running')
    job = GateJob(id=uuid.uuid4().hex, project_id=project.id, team_id=team_id, branch=branch, base=base,
                  stack_parent=stack_parent, suite=suite, priority=priority, host=project.host or 'local',
                  account=project.account, requested_by=requested_by, results={})
    db.add(job)
    db.add(AuditLog(actor=requested_by, action='gate.queue', target=job.id,
                    detail={'project': project.id, 'branch': branch, 'suite': suite, 'priority': priority, 'base': base}))
    return job


def ordered(jobs):
    """Merge jobs first, then the oldest."""
    return sorted(jobs, key=lambda j: (j.priority != 'merge', j.created_at, j.id))


def view(job: GateJob) -> dict:
    return {key: getattr(job, key) for key in ('id', 'project_id', 'team_id', 'branch', 'base', 'head', 'base_sha', 'commands_digest', 'runner_version', 'suite', 'priority',
                                                'state', 'waiting', 'slot', 'attempt', 'host', 'reason', 'results',
                                                'requested_by')} | {
        'created_at': job.created_at, 'started_at': job.started_at, 'finished_at': job.finished_at}


def _finish(db, job: GateJob, state: str, reason: str | None = None, actor: str = 'gate') -> None:
    job.state, job.finished_at, job.waiting = state, utcnow(), None
    job.reason = (reason or job.reason or '')[:500] or None
    db.add(AuditLog(actor=actor, action='gate.' + {'cancelled': 'cancel'}.get(state, 'finish'), target=job.id,
                    detail={'state': state, 'reason': job.reason, 'results': job.results, 'branch': job.branch}))


def cancel(db, job: GateJob, actor: str, reason=None) -> bool:
    """Queued jobs end at once; a running job ends once its broker confirms the stop (Gates.tick)."""
    if job.state == 'queued':
        _finish(db, job, 'cancelled', reason or 'cancelled before it started', actor)
        return True
    if job.state == 'running':
        job.waiting = 'cancel'
        if reason:
            job.reason = reason
        db.add(AuditLog(actor=actor, action='gate.cancel', target=job.id, detail={'state': 'stopping'}))
        return True
    return False


def _argv(argv: list[str], tests: list[str] | None = None) -> list[str]:
    out = []
    for arg in argv:
        if arg == '{tests}':
            out.extend(tests or [])
        else:
            out.append(arg)  # {junit} is substituted by the broker with the job's own report path
    return out


class Gates:
    """The scheduler. One per supervisor, started on first use (and by the API), stopping when nothing is active."""

    def __init__(self, supervisor):
        self.supervisor = supervisor
        self.task: asyncio.Task | None = None
        self.lock = asyncio.Lock()
        self.nudge = asyncio.Event()

    def wake(self) -> None:
        """Start the loop, or cut its current wait short (a new job, a cancel, a budget change)."""
        self.nudge.set()
        if self.task is None or self.task.done():
            self.task = asyncio.get_running_loop().create_task(self.run())

    async def run(self) -> None:
        while True:
            self.nudge.clear()
            try:
                if not await self.tick():
                    return
            except Exception:
                log.exception('gate queue tick failed')
            try:
                await asyncio.wait_for(self.nudge.wait(), TICK)
            except TimeoutError:
                pass

    async def tick(self) -> bool:
        """One pass: finish what ended, then start what fits. Returns whether anything is still active."""
        sup = self.supervisor
        async with self.lock:
            with sup.db() as db:
                config = budget(db) if orchestrator_enabled() else {**budget(db), 'enabled': False}
                running = [view(j) | {'pid_record': j.pid_record, 'account': j.account} for j in db.scalars(select(GateJob).where(GateJob.state == 'running'))]
            for job in running:
                await self._poll(job)
            with sup.db() as db:
                if not config['enabled']:
                    return bool(running)
                running = list(db.scalars(select(GateJob).where(GateJob.state == 'running')))
                queued = ordered(db.scalars(select(GateJob).where(GateJob.state == 'queued')))
                used = {j.slot for j in running}
                full = sum(j.suite == 'full' for j in running)
                plan = []
                for job in queued:
                    free = [n for n in range(1, config['gate_slots'] + 1) if n not in used]
                    if not free:
                        job.waiting = 'slot'
                        continue
                    if job.suite == 'full' and full >= config['full_suites_max']:
                        job.waiting = 'full suite'
                        continue
                    plan.append((job.id, free[0]))
                    used.add(free[0])
                    full += job.suite == 'full'
                db.commit()
            for job_id, slot in plan:
                await self._start(job_id, slot, config)
            with sup.db() as db:
                return db.scalar(select(GateJob.id).where(GateJob.state.in_(ACTIVE)).limit(1)) is not None

    async def _start(self, job_id: str, slot: int, config: dict) -> None:
        sup = self.supervisor
        with sup.db() as db:
            job = db.get(GateJob, job_id)
            project = db.get(Project, job.project_id) if job else None
            if job is None or job.state != 'queued' or project is None:
                return
            team = db.get(Team, job.team_id) if job.team_id else None
            if job.team_id and (team is None or team.stopped or utcnow() >= team.expires_at):
                # A team's gate runs its branch's code as the project account: without a live team it has no authority.
                _finish(db, job, 'cancelled', 'cancelled: team stopped')
                db.commit()
                return
            if sandboxed(project, sup.settings):  # switched to containers after the job was queued
                _finish(db, job, 'refused', CONTAINER_REFUSAL)
                db.commit()
                return
            argv = commands(db, project.id)
            if argv is None:
                _finish(db, job, 'refused', 'the project gate commands were removed; queue a fresh job')
                db.commit()
                return
            host, account, repo = job.host, job.account, project.workspace_path
            suite, branch, base, team_id = job.suite, job.branch, job.base, job.team_id
        try:
            if suite == 'full':
                load = (await sup.broker.call('gate_load', account=account, host=host))['load1']
                if load > config['load_ceiling']:
                    with sup.db() as db:
                        db.get(GateJob, job_id).waiting = f'load {load:.1f} over {config["load_ceiling"]:g}'
                        db.commit()
                    return
            prepared = await sup.broker.call('gate_prepare', account=account, host=host, path=repo, job=job_id,
                                             branch=branch, base=base)
        except BrokerUnavailable:  # the machine is away: wait rather than fail on a hiccup
            with sup.db() as db:
                db.get(GateJob, job_id).waiting = 'machine unavailable'
                db.commit()
            return
        except BrokerError as error:  # the broker refused (no such branch, bad base): a person or the lead must act
            with sup.db() as db:
                _finish(db, db.get(GateJob, job_id), 'refused', f'could not prepare: {str(error)[:300]}')
                db.commit()
            return
        problems = []
        with sup.db() as db:
            job = db.get(GateJob, job_id)
            job.head = prepared.get('head')
            job.base_sha = prepared.get('base_tip')
            job.commands_digest = command_digest(argv)
            job.runner_version = prepared.get('runner_version')
            if not prepared.get('based'):
                _finish(db, job, 'needs-restack', f'{branch} is not based on the current {base}; restack it, then queue again')
            else:
                numbers = {k: set(v) for k, v in prepared.get('base_numbers', {}).items()}
                reservations.mark_used(db, numbers)
                problems = reservations.check(db, team_id, {k: set(v) for k, v in prepared.get('added', {}).items()}, numbers)
                if problems:
                    _finish(db, job, 'refused', '; '.join(problems))
            db.commit()
            state = job.state
        if state != 'queued':
            await self._cleanup(account, host, job_id)
            return
        await self._launch(job_id, slot, argv[suite], attempt=1)

    async def _launch(self, job_id: str, slot: int, argv: list[str], attempt: int, tests=None) -> None:
        sup = self.supervisor
        with sup.db() as db:
            job = db.get(GateJob, job_id)
            account, host, config = job.account, job.host, budget(db)
            project = db.get(Project, job.project_id)
            team = db.get(Team, job.team_id) if job.team_id else None
            # Preparation and status polling await the broker; Stop team or expiry can win either wait.
            if job.team_id and (team is None or team.stopped or utcnow() >= team.expires_at):
                _finish(db, job, 'cancelled', 'cancelled: team stopped')
                db.commit()
                blocked = True
            # Checked right before every start, the targeted rerun included: the project may have switched to
            # containers while the job was being prepared or running (v2 gate sandbox review, GS-2).
            elif project is None or sandboxed(project, sup.settings):
                _finish(db, job, 'refused', CONTAINER_REFUSAL)
                db.commit()
                blocked = True
            else:
                # Preparation/polling awaited the broker. Neither a stale plan nor the old argv is
                # authority to start a process after the owner paused the queue or changed commands.
                current = commands(db, job.project_id) or {}
                command_changed = (current.get('rerun' if attempt > 1 else job.suite) != argv
                                   or job.commands_digest != command_digest(current))
                # A planned slot is not a reservation across broker waits. Running jobs can retain an old slot after
                # a budget reduction, so count them too, not just the slots within the new range. A targeted rerun's
                # first process has ended: its own running row must not count against its replacement process.
                running = list(db.scalars(select(GateJob).where(GateJob.state == 'running', GateJob.id != job_id)))
                used = {other.slot for other in running}
                free = [number for number in range(1, config['gate_slots'] + 1) if number not in used]
                capacity = ('slot' if len(running) >= config['gate_slots'] or not free else
                            'full suite' if job.suite == 'full' and
                            sum(other.suite == 'full' for other in running) >= config['full_suites_max'] else None)
                blocked = not orchestrator_enabled() or not config['enabled'] or command_changed or capacity is not None
                if blocked:
                    reason = ('the project gate commands changed; queue a fresh job' if command_changed else
                              'Orchestrator v2 is off' if not orchestrator_enabled() else
                              'the gate queue is off' if not config['enabled'] else f'host budget: waiting for {capacity}')
                    if command_changed or attempt > 1:
                        _finish(db, job, 'failed' if attempt > 1 else 'refused', reason)
                    else:
                        job.waiting = capacity if orchestrator_enabled() and config['enabled'] else reason
                    db.commit()
                else:
                    slot = slot if slot in free else free[0]
        if blocked:
            await self._cleanup(account, host, job_id)
            return
        limits = {'seconds': config['job_minutes'] * 60, 'log_bytes': config['log_mib'] << 20,
                  'memory_bytes': config['memory_gib'] << 30}
        try:
            record = await sup.broker.call('gate_start', account=account, host=host, job=job_id, argv=_argv(argv, tests),
                                           limits=limits)
        except Exception as error:
            with sup.db() as db:
                job = db.get(GateJob, job_id)
                _finish(db, job, 'failed', f'could not start: {str(error)[:200]}')
                db.commit()
            await self._cleanup(account, host, job_id)
            return
        with sup.db() as db:
            job = db.get(GateJob, job_id)
            team = db.get(Team, job.team_id) if job.team_id else None
            stopped = job.waiting == 'cancel' or job.state == 'cancelled' or (job.team_id and (not team or team.stopped or utcnow() >= team.expires_at))
            job.state, job.slot, job.attempt, job.waiting = 'running', slot, attempt, 'cancel' if stopped else None
            job.finished_at = None
            if stopped:
                job.reason = 'cancelled: team stopped'
            job.pid_record = {'machine': host, 'pid': record['pid'], 'start': record['start']}
            job.log_path, job.started_at = record.get('log'), job.started_at or utcnow()
            db.add(AuditLog(actor='gate', action='gate.start', target=job_id,
                            detail={'slot': slot, 'attempt': attempt, 'suite': job.suite, 'head': job.head,
                                    'rerun': list(tests or [])}))
            db.commit()

    async def _poll(self, job: dict) -> None:
        sup = self.supervisor
        with sup.db() as db:
            row = db.get(GateJob, job['id'])
            team = db.get(Team, row.team_id) if row.team_id else None
            if row.team_id and (not team or team.stopped or utcnow() >= team.expires_at):
                cancel(db, row, 'supervisor', 'cancelled: team stopped')
                db.commit()
            job['waiting'] = row.waiting
        record = job['pid_record'] or {}
        try:
            ask = dict(account=job['account'], host=job['host'], job=job['id'], pid=record.get('pid'), start=record.get('start'))
            if job['waiting'] == 'cancel':
                await sup.broker.call('gate_stop', **ask)
                for _ in range(4):  # a stopped process usually exits within a moment: report it now, not next tick
                    if not (status := await sup.broker.call('gate_status', **ask)).get('running'):
                        break
                    await asyncio.sleep(0.5)
            else:
                status = await sup.broker.call('gate_status', **ask)
        except Exception:
            return  # the machine may be briefly away; the record stays and the next tick asks again
        if status.get('running'):
            return
        with sup.db() as db:
            row = db.get(GateJob, job['id'])
            if row.waiting == 'cancel':
                _finish(db, row, 'cancelled', row.reason or 'stopped by a person')
                db.commit()
                return await self._cleanup(row.account, row.host, row.id)
            if status.get('timed_out'):  # stopped at the host budget's time limit: never a pass, never rerun
                row.results = {'exit': status.get('exit'), 'counts': status.get('counts') or {}, 'failed': []}
                _finish(db, row, 'failed', 'stopped at the host budget\'s time limit for one gate job')
                db.commit()
                return await self._cleanup(row.account, row.host, row.id)
            exit_code, failed = status.get('exit'), list(status.get('failed') or [])
            first = dict(row.results or {})
            results = {'exit': exit_code, 'counts': status.get('counts') or {}, 'failed': failed[:RERUN_LIMIT]}
            argv = commands(db, row.project_id) or {}
            if row.attempt == 1:
                row.results = results
                if exit_code == 0:
                    _finish(db, row, 'passed')
                elif exit_code is None:
                    _finish(db, row, 'failed', 'the test process ended without an exit status (machine or broker restart)')
                elif status.get('rerunnable') and len(failed) <= RERUN_LIMIT and argv.get('rerun') and _ids_ok(failed):
                    db.commit()
                    return await self._launch(row.id, row.slot, argv['rerun'], attempt=2, tests=failed)
                else:
                    _finish(db, row, 'failed', 'suite failed' if failed else 'suite failed without a test report')
            else:
                row.results = {**first, 'rerun': results}
                if exit_code == 0:
                    row.results = {**row.results, 'flaky': first.get('failed', [])}
                    known = set(first.get('failed') or [])
                    state = 'known-flake' if known and known <= set(flakes(db, row.project_id)) else 'retry-passed'
                    _finish(db, row, state, 'failed tests passed on a targeted rerun')
                else:
                    _finish(db, row, 'failed', 'failed again on the targeted rerun')
            db.commit()
            account, host, job_id = row.account, row.host, row.id
        await self._cleanup(account, host, job_id)

    async def _cleanup(self, account: str, host: str, job_id: str) -> None:
        try:
            await self.supervisor.broker.call('gate_cleanup', account=account, host=host, job=job_id)
        except Exception:
            log.warning('gate %s: cleanup on %s failed; the broker retries on its next prepare', job_id, host)


TEST_ID = re.compile(r'^[A-Za-z0-9_./:\[\]-][A-Za-z0-9_./:\[\]@ =,+-]{0,299}$')


def _ids_ok(ids: list[str]) -> bool:
    return all(isinstance(i, str) and TEST_ID.fullmatch(i) and not i.startswith('-') for i in ids)


def gates(supervisor) -> Gates:
    return supervisor.__dict__.setdefault('_orchestrator_gates', Gates(supervisor))


# ---- team tools (acc-mcp-team GATE_TOOLS) -------------------------------------------------------------------------

def _caller(db, supervisor, caller_id):
    from .. import teams
    caller = supervisor._get(db, caller_id)
    team = db.get(Team, caller.team_id) if caller.team_id else None
    if not team or team.stopped or utcnow() >= team.expires_at or caller.desired_state != 'running':
        raise LaunchRefused('team is stopped, expired, or caller is not running')
    return caller, teams.descendants(db, caller)


def _queue_on(db) -> None:
    """Reservations and restacks belong to the gate queue: while an owner hasn't turned it on, they don't exist either
    (a restack is allowed on the lead's own only because the gate then reruns the helper's tests)."""
    if not orchestrator_enabled():
        raise LaunchRefused('Orchestrator v2 is off')
    if not budget(db)['enabled']:
        raise LaunchRefused('the gate queue is off; an owner turns it on in the host budget')


def _member(caller, children, helper_id):
    if helper_id in (None, '', caller.id):
        return caller
    found = next((x for x in children if x.id == helper_id), None)
    if found is None:
        raise LaunchRefused('helper is outside caller subtree')
    return found


async def reserve_tool(supervisor, caller_id, payload):
    with supervisor.db() as db:
        caller, _ = _caller(db, supervisor, caller_id)
        _queue_on(db)
        where = dict(account=caller.run_as, host=caller.host, path=caller.team_repo or caller.project.workspace_path)
    found = await supervisor.broker.call('gate_numbers', **where)
    landed = {k: set(found['base'].get(k, [])) for k in reservations.KINDS}
    pending = {k: set(found['pending'].get(k, [])) for k in reservations.KINDS}  # unmerged topic branches
    with supervisor.db() as db:
        caller, _ = _caller(db, supervisor, caller_id)
        _queue_on(db)  # the owner may have disabled the queue while gate_numbers was in flight
        row = reservations.reserve(db, payload.get('kind'), caller, landed, pending)
        result = reservations.view(row)
        db.commit()
    return result


async def release_tool(supervisor, caller_id, payload):
    with supervisor.db() as db:
        caller, _ = _caller(db, supervisor, caller_id)
        _queue_on(db)
        result = reservations.view(reservations.release(db, payload.get('reservation'), caller))
        db.commit()
    return result


async def gate_tool(supervisor, caller_id, payload):
    suite, priority = payload.get('suite', 'related'), payload.get('priority', 'check')
    with supervisor.db() as db:
        caller, children = _caller(db, supervisor, caller_id)
        member = _member(caller, children, payload.get('helper'))
        if suite == 'full' and caller.team_depth != 0:
            raise LaunchRefused('only the lead queues full suites; helpers run related tests')
        if member.project_id is None or member.worktree_branch is None:
            raise LaunchRefused('that member has no project branch to gate')
        if member.host != member.project.host:
            raise LaunchRefused('gates run on the project machine; merge the remote helper first')
        base = 'main' if member.id == caller.team_id else (db.get(Session, member.parent_session_id).worktree_branch or 'main')
        job = enqueue(db, member.project, member.worktree_branch, suite, priority, f'session:{caller_id}',
                      settings=supervisor.settings, team_id=caller.team_id, base=base)
        result = view(job)
        db.commit()
    gates(supervisor).wake()
    return result


async def gate_status_tool(supervisor, caller_id, payload):
    with supervisor.db() as db:
        caller, children = _caller(db, supervisor, caller_id)
        job = db.get(GateJob, payload.get('job')) if isinstance(payload.get('job'), str) else None
        branches = {caller.worktree_branch, *(x.worktree_branch for x in children)}
        if job is None or job.team_id != caller.team_id or job.branch not in branches:
            raise LaunchRefused('no gate job with that id for your branch or your helpers')
        result = view(job)
        project = db.get(Project, job.project_id)
        command_set = commands(db, job.project_id)
        where = dict(account=job.account, host=job.host, path=project.workspace_path)
    try:
        heads = (await supervisor.broker.call('gate_heads', **where, branches=sorted({job.branch, job.base})))['heads']
    except Exception:
        heads = {}
    result['stale'] = stale_reason(job, heads, command_set) if job.head else None
    return result


async def restack_tool(supervisor, caller_id, payload):
    """The owner's decision (design section 10, question 4): the lead may replay a helper's own commits by itself; a
    conflict refuses and changes nothing; the gate then reruns the helper's tests. Merging to main stays a person's."""
    onto = payload.get('onto', 'lead')
    if onto not in ('lead', 'main'):
        raise LaunchRefused('onto is lead (your current branch) or main')
    with supervisor.db() as db:
        caller, children = _caller(db, supervisor, caller_id)
        if caller.team_depth != 0:
            raise LaunchRefused('only the lead restacks helpers')
        _queue_on(db)
        helper = next((x for x in children if x.id == payload.get('helper')), None)
        if helper is None:
            raise LaunchRefused('helper is outside caller subtree')
        if helper.desired_state == 'running' or helper.team_stop_pending:
            raise LaunchRefused('stop the helper and wait for confirmation before restacking its branch')
        if helper.host != caller.host:
            raise LaunchRefused('restack runs where both branches are; merge or bring back the remote helper first')
        if helper.project_id is None or commands(db, helper.project_id) is None:
            raise LaunchRefused('restack needs the project\'s gate commands, so the gate can rerun the helper\'s tests')
        parent = db.get(Session, helper.parent_session_id)
        args = dict(account=caller.run_as, host=caller.host, path=helper.cwd, branch=helper.worktree_branch,
                    parent=parent.worktree_branch, onto='main' if onto == 'main' else caller.worktree_branch)
        helper_id, project = helper.id, helper.project
    result = await supervisor.broker.call('gate_restack', **args)
    with supervisor.db() as db:
        caller = supervisor._get(db, caller_id)
        db.add(AuditLog(actor=f'session:{caller_id}', action='team.restack', target=helper_id,
                        detail={'onto': args['onto'], **{k: result.get(k) for k in ('restacked', 'head', 'conflicts', 'replayed')}}))
        queued = None
        if result.get('restacked'):
            try:
                queued = view(enqueue(db, project, args['branch'], 'related', 'check', f'session:{caller_id}',
                                      settings=supervisor.settings, team_id=caller.team_id, base=args['onto']))
            except LaunchRefused:
                queued = None  # already queued
        db.commit()
    if queued:
        gates(supervisor).wake()
    return {**result, 'gate': queued}


TEAM_FIELDS: dict[str, set[str]] = {'gate': {'helper', 'suite', 'priority'}, 'gate_status': {'job'},
                                    'restack': {'helper', 'onto'}}
TEAM_ACTIONS: dict = {'reserve': reserve_tool, 'release': release_tool, 'gate': gate_tool,
                      'gate_status': gate_status_tool, 'restack': restack_tool}
ON_STATE: list = []
