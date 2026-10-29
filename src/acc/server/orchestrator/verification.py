"""Commit-bound evidence and bounded team shutdown; no authority comes from report text."""
from datetime import timedelta

from ..models import Setting, utcnow
from ..supervisor_common import LaunchRefused

WRAPUP_KEY = 'orchestrator.wrapup_minutes'
WRAPUP_ACTIONS = frozenset({'report', 'plan_update', 'worklist_add', 'worklist_update',
                            'worklist_ingest', 'worklist_done', 'finish'})


def wrapup_minutes(db):
    row = db.get(Setting, WRAPUP_KEY)
    return row.value if row else 30


def wrapup(team, caller, at=None):
    return bool(team and caller and team.wrapup_until and (at or utcnow()) < team.wrapup_until
                and caller.id == team.id and caller.desired_state == 'running')


def allowed(team, caller, action, at=None):
    at = at or utcnow()
    return bool(team and caller and caller.desired_state == 'running' and (
        (not team.stopped and at < team.expires_at) or
        (wrapup(team, caller, at) and action in WRAPUP_ACTIONS)))


def require_action(db, caller, action):
    from ..models import Team
    team = db.get(Team, caller.team_id) if caller and caller.team_id else None
    if not allowed(team, caller, action):
        raise LaunchRefused('team is stopped, expired, or caller is not running (wrap-up permits reports, plan updates, working-list changes and finish only)')
    return team


def deadline(db, team):
    # Bound to the original expiry, never to a delayed supervisor pass/restart.
    return team.expires_at + timedelta(minutes=wrapup_minutes(db))


def stop_state(session):
    if session.team_stop_pending:
        return 'stop requested'
    if session.desired_state == 'archived':
        return 'finished'
    if session.desired_state == 'stopped':
        return 'stop confirmed'
    return None


def review_fields(payload):
    import re
    branch, head = payload.get('reviewed_branch'), payload.get('reviewed_head')
    if branch is None and head is None:
        return {'reviewed_branch': None, 'reviewed_head': None}
    from .gates import BRANCH
    if not isinstance(branch, str) or not BRANCH.fullmatch(branch) or not isinstance(head, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', head):
        raise ValueError('A review needs the reviewed team branch and its full commit SHA.')
    return {'reviewed_branch': branch, 'reviewed_head': head}


def review_evidence(reports, head, branch=None):
    # Shared lane C interface: (attempt_id, version) supersedes older reports.
    # Legacy rows have one report per (session_id, task_id).
    latest = {}
    for row in reports:
        attempt = getattr(row, 'attempt_id', None)
        key = ('attempt', attempt) if attempt else ('legacy', row.session_id, getattr(row, 'task_id', None))
        previous = latest.get(key)
        if previous is None or getattr(row, 'version', 0) >= getattr(previous, 'version', 0):
            latest[key] = row
    matched = [r for r in latest.values() if head and r.reviewed_head == head and r.status == 'done'
               and (branch is None or r.reviewed_branch == branch)]
    return {'status': 'reviewed at this revision' if matched else 'not reviewed at this revision',
            'head': head, 'reviewers': [r.session_id for r in matched]}


async def evidence(supervisor, member):
    """Same projection for the lead's view and integration. Unknown freshness never authorizes a pass."""
    from sqlalchemy import select

    from ..models import Project
    from . import gates
    from .delivery import TaskReport
    with supervisor.db() as db:
        project = db.get(Project, member.project_id)
        if project is None or not member.worktree_branch:
            return {'gate': None, 'review': {'status': 'not reviewed at this revision'}, 'blocking': None}
        job = db.scalar(select(gates.GateJob).where(gates.GateJob.project_id == member.project_id,
                        gates.GateJob.branch == member.worktree_branch).order_by(gates.GateJob.created_at.desc()).limit(1))
        command_set = gates.commands(db, member.project_id)
        reports = list(db.scalars(select(TaskReport).where(TaskReport.team_id == member.team_id)))
        passes = gates.integration_pass(db, job) if job else False
        branches = {member.worktree_branch}
        if job:
            branches.add(job.base)
        where = dict(account=member.run_as, host=member.host, path=member.team_repo or project.workspace_path)
    try:
        heads = (await supervisor.broker.call('gate_heads', **where, branches=sorted(branches)))['heads']
    except Exception:
        heads = {}
    review = review_evidence(reports, heads.get(member.worktree_branch), member.worktree_branch)
    gate = gates.view(job) if job else None
    blocking = None
    if job:
        stale = gates.stale_reason(job, heads, command_set)
        gate['stale'] = stale
        if not passes:
            blocking = 'gate ' + job.state
        elif stale is not False:
            blocking = stale or 'gate freshness unconfirmed'
    return {'gate': gate, 'review': review, 'blocking': blocking}
