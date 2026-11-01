"""Person-controlled lifetime and a fenced preview of saved-session resumption."""
import hashlib
import json
from datetime import timedelta

from sqlalchemy import select

from .. import providers
from . import attention, budgets
from .compiler import LaunchInputs
from .models import AuditLog, Event, Session, SessionRun, Team
from .supervisor_common import LaunchRefused, now


def hours(value):
    if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 168):
        raise LaunchRefused('Hours must be empty for no limit, or a number above 0 and at most 168.')
    return value


def deadline(value, base=None):
    return (base or now()) + timedelta(hours=hours(value)) if value is not None else None


def eligible(db, session):
    if session.id == session.team_id and session.parent_session_id is None:
        return session.management == 'acc' and session.desired_state != 'archived'
    if session.management != 'acc' or session.desired_state == 'archived' or session.observed_state in ('recovery_needed', 'failed', 'error', 'exited'):
        return False
    from .orchestrator.assignments import current, replaced
    from .orchestrator.delivery import TaskReport
    attempt = current(db, session.id)
    if attempt and (attempt.state in ('failed', 'reported') or replaced(db, attempt)):
        return False
    report = db.scalar(select(TaskReport).where(TaskReport.session_id == session.id,
        TaskReport.status.in_(('done', 'failed')), TaskReport.attempt_id == attempt.id if attempt else TaskReport.attempt_id.is_(None)).limit(1))
    return report is None


def capture(db, team, members):
    team.resume_members = [s.id for s in members if s.desired_state == 'running' and eligible(db, s)]
    team.lifetime_revision += 1


def candidate_ids(db, team):
    if team.resume_members or team.lifetime_revision or not team.stopped:
        return team.resume_members
    # Pre-upgrade stopped teams have no snapshot. Recover only members whose
    # recorded pre-stop state proves they were active; never guess a failed helper.
    stop = db.scalar(select(AuditLog).where(AuditLog.target == team.id,
        AuditLog.action.in_(('team.expired', 'team.stop'))).order_by(AuditLog.id.desc()).limit(1))
    if stop is None:
        lead = db.get(Session, team.id)
        return [team.id] if lead and eligible(db, lead) else []
    if stop.actor == 'supervisor' and stop.action == 'team.stop':
        expiry = db.scalar(select(AuditLog).where(AuditLog.target == team.id, AuditLog.action == 'team.expired').order_by(AuditLog.id.desc()).limit(1))
        if expiry is not None:
            stop = expiry  # use the pre-expiry state, not the later wrap-up stop
    lead = db.get(Session, team.id)
    ids = [team.id] if lead and eligible(db, lead) else []
    for session in db.scalars(select(Session).where(Session.team_id == team.id)):
        if session.id != team.id:
            run = db.scalar(select(SessionRun).where(SessionRun.session_id == session.id, SessionRun.started_at <= stop.ts).order_by(SessionRun.id.desc()).limit(1))
            if run is None or run.verified_at is None or (run.ended_at is not None and run.ended_at < stop.ts):
                continue
        event = db.scalar(select(Event).where(Event.session_id == session.id, Event.kind == 'state',
            Event.ts <= stop.ts).order_by(Event.id.desc()).limit(1))
        if event and event.payload.get('to') in ('idle', 'running', 'waiting_input', 'starting', 'recovering'):
            if session.id not in ids:
                ids.append(session.id)
        elif session.id == team.id and session.desired_state == 'running':
            if session.id not in ids:
                ids.append(session.id)
    return ids


def permission_summary(session):
    """Only registry-declared, human-readable choices; never arbitrary saved inputs."""
    inputs = LaunchInputs.from_dict(session.launch_spec.inputs)
    provider = providers.get(session.provider)
    summary = {'Profile': inputs.profile} if inputs.profile else {}
    for option in provider.options:
        value = provider.option(inputs, option.key)
        if option.kind == 'bool':
            label = ('On' if value else 'Off') if type(value) is bool else 'Unrecognised saved choice'
        else:
            label = next((label for choice, label in option.choices if choice == value), 'Unrecognised saved choice')
        summary[option.label] = label
    return {'permissions': summary, 'dangerous_permissions': provider.dangerous(inputs)}


def preview(db, team):
    members = list(db.scalars(select(Session).where(Session.team_id == team.id)))
    ids = candidate_ids(db, team)
    chosen = [s for s in members if s.id in ids and eligible(db, s)
              and (team.stopped or s.desired_state != 'running')]
    detail = [(s.id, s.desired_state, s.team_stop_pending, s.launch_spec.inputs) for s in chosen]
    revision = hashlib.sha256(json.dumps([team.lifetime_revision, team.stopped, team.expires_at.isoformat() if team.expires_at else None, detail], sort_keys=True).encode()).hexdigest()
    return {'revision': revision, 'sessions': [{'id': s.id, 'name': s.name, 'provider': s.provider,
        **permission_summary(s),
        'pending_stop': s.team_stop_pending} for s in chosen]}


def set_limit(db, team, actor, value):
    value = hours(value)
    previous = team.expires_at
    team.expires_at = deadline(value, max(now(), previous) if previous else now())
    team.limits = {**team.limits, 'hours': value}
    team.lifetime_revision += 1
    attention.resolve(db, 'team_expiring:' + team.id)
    db.add(AuditLog(actor=actor, action='team.remove_limit' if value is None else 'team.extend', target=team.id,
        detail={'hours': value, 'previous_expires_at': previous.isoformat() if previous else None,
                'expires_at': team.expires_at.isoformat() if team.expires_at else None}))


async def resume(supervisor, team_id, actor, revision, value):
    from contextlib import AsyncExitStack
    # Match ordinary resume's workspace-before-team lock order. Holding all
    # selected folders prevents deletion from racing the whole confirmation.
    with supervisor.db() as db:
        team = db.get(Team, team_id)
        if team is None:
            raise LaunchRefused('No such team.')
        plan = preview(db, team)
        paths = set()
        for row in plan['sessions']:
            session = db.get(Session, row['id'])
            if session.project is None or session.project.deleting or session.project.archived:
                raise LaunchRefused('A selected project is unavailable or being deleted.')
            paths.add((session.host, session.workdir or session.project.workspace_path))
    async with AsyncExitStack() as stack:
        for host, path in sorted(paths):
            await stack.enter_async_context(supervisor.workspace_operations.hold(path, host, kind='resume'))
        return await _resume_locked(supervisor, team_id, actor, revision, value)


async def _resume_locked(supervisor, team_id, actor, revision, value):
    import asyncio
    async with supervisor.__dict__.setdefault('_team_lock', asyncio.Lock()):
        with supervisor.db() as db:
            team = db.get(Team, team_id)
            if team is None:
                raise LaunchRefused('No such team.')
            plan = preview(db, team)
            if revision != plan['revision'] or not plan['sessions']:
                raise LaunchRefused('The resume list changed. Review it again before confirming.')
            sessions = [db.get(Session, row['id']) for row in plan['sessions']]
            from .orchestrator import assignments, planning
            for session in sessions:
                if session.project is None or session.project.deleting or session.project.archived:
                    raise LaunchRefused('A selected project is unavailable or being deleted.')
                if session.team_stop_pending:
                    raise LaunchRefused('Wait for every selected machine to confirm its stop, then review Resume again.')
                if budgets.paused(db, session.owner, session.provider):
                    raise LaunchRefused('Your budget pauses a selected agent. Check the budget before resuming.')
                planning.require_resume(db, session)
                if session.parent_session_id:
                    assignments.require_resume(db, session)
            # No limit is a deliberate, explicit person choice, not an agent mutation.
            previous_deadline = team.expires_at
            team.expires_at = deadline(value)
            team.limits = {**team.limits, 'hours': hours(value)}
            team.stopped = False
            team.wrapup_until = None
            team.stop_reason = None
            team.lifetime_revision += 1
            attention.resolve(db, 'team_expired:' + team.id)
            attention.resolve(db, 'team_expiring:' + team.id)
            db.add(AuditLog(actor=actor, action='team.remove_limit' if value is None else 'team.extend', target=team.id,
                detail={'reason': 'resume', 'hours': value, 'previous_expires_at': previous_deadline.isoformat() if previous_deadline else None,
                        'expires_at': team.expires_at.isoformat() if team.expires_at else None}))
            db.add(AuditLog(actor=actor, action='team.resume', target=team.id,
                detail={'sessions': [s.id for s in sessions], 'hours': value}))
            db.commit()
        resumed, errors = [], {}
        for row in plan['sessions']:
            try:
                # _resume rechecks saved placement, permissions, assignment and concurrency.
                # The caller already holds workspace leases and the team mutation lock.
                with supervisor.db() as db:
                    running = db.get(Session, row['id']).desired_state == 'running'
                if not running:
                    await supervisor._resume(row['id'], actor)
                resumed.append(row['id'])
            except LaunchRefused as error:
                errors[row['id']] = str(error)
        with supervisor.db() as db:
            team = db.get(Team, team_id)
            team.resume_members = list(errors)
            db.commit()
        return {'resumed': resumed, 'errors': errors}
