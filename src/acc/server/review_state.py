"""Read-only review signals, derived from existing session and GitHub observations."""
from sqlalchemy import func, or_, select

from .github import same_repo
from .models import API_PRINCIPAL, AttentionItem, Event, Observation, Project, SessionRun, User


def ready_for_review(session, db=None) -> bool:
    changes = session.changes or {}
    ready = bool(session.management != 'external' and session.desired_state != 'archived'
                and session.observed_state in ('idle', 'exited') and not session.activity and session.status_at
                and session.changes_at and session.changes_at >= session.status_at
                and changes.get('git') and changes.get('branch')
                and changes['branch'] not in ('main', 'master')
                and changes.get('comparison_ref') not in (changes['branch'], 'origin/' + changes['branch'])
                and (changes.get('files', 0) or changes.get('commits', 0)))
    if ready and db is not None:
        run = db.scalar(select(SessionRun).where(SessionRun.session_id == session.id)
                        .order_by(SessionRun.id.desc()).limit(1))
        if run is None:
            return False
        # SessionStart is idle too. Only a completed turn or normal process exit counts.
        if session.observed_state != 'exited' and not db.scalar(select(Event.id).where(
                Event.session_id == session.id, Event.ts >= run.started_at,
                or_(Event.kind == 'Stop', (Event.kind == 'state') &
                    (Event.payload['to'].as_string() == 'idle') &
                    (Event.payload['from'].as_string() == 'active'))).limit(1)):
            return False
        return not db.scalar(select(AttentionItem.id).where(
            AttentionItem.session_id == session.id, AttentionItem.kind.in_(('needs_input', 'recovery_needed')),
            AttentionItem.resolved_at.is_(None)).limit(1))
    return ready


def sync_notice(db, session):
    from . import attention
    prefix = f'review:{session.id}:'
    ready = ready_for_review(session, db)
    run = db.scalar(select(SessionRun.id).where(SessionRun.session_id == session.id)
                    .order_by(SessionRun.id.desc()).limit(1))
    key = prefix + str(run)
    for item in db.scalars(select(AttentionItem).where(
            AttentionItem.session_id == session.id, AttentionItem.kind == 'ready_for_review',
            AttentionItem.resolved_at.is_(None))):
        if not ready or item.dedupe_key != key:
            item.resolved_at = attention.now()
    # Include dismissed and resolved rows: never recreate a notice for the same run.
    if ready and run is not None and not db.scalar(select(AttentionItem.id).where(AttentionItem.dedupe_key == key)):
        attention.raise_item(db, key, kind='ready_for_review', severity='info', source='workspace changes',
                             session_id=session.id, title=f'{session.name}: Ready for review',
                             detail='The agent finished with branch changes. Open Changes to review them.')


def pull_lifecycle(pull, checks=(), reviews=()):
    if pull.get('merged') or pull.get('merged_at') or pull.get('state') == 'merged':
        return 'merged'
    if pull.get('state') == 'closed':
        return 'closed'
    if pull.get('draft'):
        return 'draft'
    latest = {}
    for review in reviews:
        user = (review.get('user') or {}).get('login')
        if user and review.get('state') in ('APPROVED', 'CHANGES_REQUESTED', 'DISMISSED'):
            latest[user] = review['state']
    if 'CHANGES_REQUESTED' in latest.values():
        return 'changes_requested'
    if 'APPROVED' in latest.values():
        return 'approved'
    if any(c.get('status') != 'completed' for c in checks):
        return 'checks_running'
    if any(c.get('conclusion') in ('failure', 'timed_out', 'startup_failure', 'action_required', 'cancelled') for c in checks):
        return 'checks_failed'
    if checks and all(c.get('conclusion') in ('success', 'neutral', 'skipped') for c in checks):
        return 'checks_passed'
    return 'open'


def cached_pulls(db, project_id):
    project = db.get(Project, project_id) if db and project_id else None
    if not project or project.private_workspace or not (
            project.owner in (None, API_PRINCIPAL) or
            db.scalar(select(User.role).where(User.username == project.owner)) == 'owner'):
        return []
    observation = db.get(Observation, f'github.{project_id}')
    snapshot = observation.value or {} if observation else {}
    if not same_repo((snapshot.get('repo') or {}).get('full_name'), project.repo):
        return []
    return snapshot.get('pulls', [])


def linked_pull(db, session):
    branch = (session.changes or {}).get('branch') or session.worktree_branch
    pulls = sorted(cached_pulls(db, session.project_id), key=lambda p: p.get('state', 'open') != 'open')
    return next(({'number': p['number'], 'url': p['url'], 'state': p.get('lifecycle', 'draft' if p.get('draft') else 'open')}
                 for p in pulls if branch and p.get('head') == branch
                 and same_repo(p.get('head_repo'), session.project.repo if session.project else None)), None)


def session_review_signals(db, sessions, *, projects=None):
    """A fixed number of reads for a visible session list; never load per-session history."""
    result = {s.id: {'ready_for_review': False, 'pull_lifecycle': None} for s in sessions}
    if not sessions:
        return result
    project_ids = {s.project_id for s in sessions if s.project_id}
    if projects is None:
        projects = {p.id: p for p in db.scalars(select(Project).where(Project.id.in_(project_ids)))} if project_ids else {}
    owners = {p.owner for p in projects.values() if p.owner not in (None, API_PRINCIPAL)}
    roles = dict(db.execute(select(User.username, User.role).where(User.username.in_(owners))).all()) if owners else {}
    keys = [f'github.{pid}' for pid in projects]
    snapshots = {o.key: o.value or {} for o in db.scalars(select(Observation).where(Observation.key.in_(keys)))} if keys else {}
    pulls = {}
    for pid, project in projects.items():
        snapshot = snapshots.get(f'github.{pid}', {})
        if project.private_workspace or (project.owner not in (None, API_PRINCIPAL) and roles.get(project.owner) != 'owner'):
            continue
        if not same_repo((snapshot.get('repo') or {}).get('full_name'), project.repo):
            continue
        for pull in sorted(snapshot.get('pulls', []), key=lambda p: p.get('state', 'open') != 'open'):
            if same_repo(pull.get('head_repo'), project.repo):
                pulls.setdefault((pid, pull.get('head')), {'number': pull['number'], 'url': pull['url'],
                    'state': pull.get('lifecycle', 'draft' if pull.get('draft') else 'open')})
    for session in sessions:
        branch = (session.changes or {}).get('branch') or session.worktree_branch
        result[session.id]['pull_lifecycle'] = pulls.get((session.project_id, branch)) if branch else None
    candidates = {s.id: s for s in sessions if ready_for_review(s)}
    if not candidates:
        return result
    latest = select(func.max(SessionRun.id).label('id')).where(
        SessionRun.session_id.in_(candidates)).group_by(SessionRun.session_id).subquery()
    runs = {r.session_id: r for r in db.scalars(select(SessionRun).join(latest, SessionRun.id == latest.c.id))}
    completed = set(db.scalars(select(Event.session_id).join(SessionRun, SessionRun.session_id == Event.session_id)
        .join(latest, SessionRun.id == latest.c.id).where(Event.ts >= SessionRun.started_at,
            or_(Event.kind == 'Stop', (Event.kind == 'state') & (Event.payload['to'].as_string() == 'idle') &
                (Event.payload['from'].as_string() == 'active'))).distinct()))
    pending = set(db.scalars(select(AttentionItem.session_id).where(AttentionItem.session_id.in_(candidates),
        AttentionItem.kind.in_(('needs_input', 'recovery_needed')), AttentionItem.resolved_at.is_(None)).distinct()))
    for sid, session in candidates.items():
        result[sid]['ready_for_review'] = sid in runs and sid not in pending and (session.observed_state == 'exited' or sid in completed)
    return result
