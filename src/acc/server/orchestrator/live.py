"""Slice 2: transaction-owned tree events. No scheduler, worker or new authority.

The factory listeners run inside the writer's transaction, including member creation
before launch. A rolled-back launch/state change consequently cannot publish an event.
"""
from __future__ import annotations

from sqlalchemy import event, inspect, select, text

TEAM_FIELDS: dict[str, set[str]] = {}
TEAM_ACTIONS: dict = {}
# Separate from migration, writer, leadership and HA activity lock domains.
EVENT_ORDER_LOCK = 1094927185
KINDS = {
    'team.member.created': {'id', 'parent', 'depth', 'name', 'agent', 'profile', 'model', 'machine', 'selection'},
    'team.member.state': {'id', 'observed', 'needs_you', 'activity', 'stop_state'},
    'team.member.report': {'id', 'status', 'attempt', 'version'},
    'team.member.finished': {'id', 'merged', 'archived'},
    'team.delivery': {'id', 'session', 'state', 'attempt'},
    'team.selection': {'id', 'chosen', 'reason', 'state'},
    'worklist.item': {'id', 'state', 'revision'},
    'worklist.now': {'revision'},
    'gate.job': {'id', 'state', 'slot'},
}


def observed(row):
    return {'recovering': 'starting', 'recovery_needed': 'failed', 'exited': 'stopped',
            'archived': 'stopped'}.get(row.observed_state, row.observed_state)


def state_payload(row):
    from .verification import stop_state
    return {'id': row.id, 'observed': observed(row), 'needs_you': row.observed_state == 'waiting_input',
            'activity': None, 'stop_state': stop_state(row)}


def on_state(db, session, previous, state):
    if session.team_id:
        from ..models import Event
        db.add(Event(source='orchestrator', kind='team.member.state', session_id=session.id,
                     payload=state_payload(session)))
        if state in {'stopped', 'exited'} and previous not in {'stopped', 'exited', 'archived'}:
            db.add(Event(source='orchestrator', kind='team.member.finished', session_id=session.id,
                         payload={'id': session.id, 'merged': False, 'archived': session.desired_state == 'archived'}))


ON_STATE = [on_state]


def clean(value, redact=lambda value: value):
    """Only bounded JSON scalars/lists; never accept extra provider fields or HTML."""
    if isinstance(value, str):
        from ..nodes import node_text  # no control, bidi or zero-width characters (review OV-9)
        return node_text(redact(value), 500)
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, list):
        return [clean(item, redact) for item in value[:32] if isinstance(item, (str, bool, int))]
    return None


def payload(kind, data, redact=lambda value: value):
    if not isinstance(data, dict):
        return {}
    return {key: clean(value, redact) for key, value in data.items() if key in KINDS.get(kind, ())}


def member_data(row, redact=lambda value: value):
    inputs = row.launch_spec.inputs if row.launch_spec else {}
    return dict(payload('team.member.created', {
        'id': row.id, 'parent': row.parent_session_id, 'depth': row.team_depth,
        'name': (row.name or '')[:64], 'agent': row.provider, 'profile': inputs.get('profile'),
        'model': inputs.get('model'), 'machine': row.host, 'selection': inputs.get('selection'),
    }, redact), **dict(state_payload(row), activity=clean(row.activity, redact),
        archived=row.desired_state == 'archived'))


def install(ctx):
    """Attach once per application factory; never to unrelated/global SQLAlchemy sessions."""
    if getattr(ctx.db, '_team_live_installed', False):
        return
    ctx.db._team_live_installed = True
    from ..models import AuditLog, Event, Session

    def redactor(db, row):
        with db.no_autoflush:
            return ctx.supervisor.vault.redactor(db, row.vault_keys).text

    @event.listens_for(ctx.db, 'before_flush')
    def collect(db, flush_context, instances):
        from ..admin_features import enabled
        if not enabled(ctx.settings, 'orchestrator'):
            return
        pending = []
        for row in list(db.new) + list(db.dirty):
            if isinstance(row, Session) and row.team_id:
                state = inspect(row)
                if row in db.new:
                    pending.append(('team.member.created', row, None))
                else:
                    if any(state.attrs[key].history.has_changes() for key in ('activity', 'team_stop_pending', 'desired_state')):
                        existing = next((e for e in db.new if isinstance(e, Event) and e.session_id == row.id
                                         and e.kind == 'team.member.state' and e.source == 'orchestrator'), None)
                        data = dict(state_payload(row), activity=clean(row.activity, redactor(db, row)))
                        if existing:
                            existing.payload = data
                        else:
                            pending.append(('team.member.state', row, data))
                    if state.attrs.desired_state.history.has_changes() and row.desired_state == 'archived':
                        pending.append(('team.member.finished', row, {'id': row.id, 'archived': True, 'merged': False}))
            elif isinstance(row, AuditLog) and row in db.new and row.action == 'team.merge':
                member = db.get(Session, (row.detail or {}).get('helper'))
                if member and member.team_id == row.target:
                    pending.append(('team.member.finished', member, {'id': member.id,
                                    'merged': bool((row.detail or {}).get('merged')), 'archived': member.desired_state == 'archived'}))
        for entry in list(db.new):
            if isinstance(entry, Event) and entry.source == 'orchestrator' and entry.kind == 'team.member.state':
                member = db.get(Session, entry.session_id)
                if member:
                    entry.payload = dict(state_payload(member), activity=clean(member.activity, redactor(db, member)))
        db.info.setdefault('team_live_pending', []).extend(pending)
        # PostgreSQL sequences alone do NOT order commits. Allocate orchestrator
        # event IDs behind one transaction lock, so a cursor cannot skip a late
        # lower-ID commit. Readers never take this lock. SQLite serializes writers.
        if pending or any(isinstance(e, Event) and e.source == 'orchestrator' for e in db.new):
            if db.get_bind().dialect.name == 'postgresql':
                db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': EVENT_ORDER_LOCK})

    @event.listens_for(ctx.db, 'after_flush_postexec')
    def publish(db, flush_context):
        for kind, row, data in db.info.pop('team_live_pending', []):
            if data is None:
                data = {key: value for key, value in member_data(row, redactor(db, row)).items()
                        if key in KINDS[kind]}
            db.add(Event(source='orchestrator', kind=kind, session_id=row.id, payload=data))

    @event.listens_for(ctx.db, 'after_soft_rollback')
    def discard(db, previous):
        db.info.pop('team_live_pending', None)


def members(db, lead):
    """Only a connected tree owned by the lead's person; corrupt/orphan rows fail closed."""
    from ..models import Session
    rows = {r.id: r for r in db.scalars(select(Session).where(Session.team_id == lead.id,
                                                           Session.owner == lead.owner))}
    result, frontier = [], [lead.id]
    seen = set()
    while frontier:
        id = frontier.pop(0)
        if id in seen or id not in rows:
            continue
        seen.add(id)
        result.append(rows[id])
        frontier.extend(sorted(r.id for r in rows.values() if r.parent_session_id == id))
    return result


def details(db, rows, scope, redactors):
    """Latest sibling-slice projections; bounded rows, no dependency on their tables."""
    from sqlalchemy import func

    from ..models import Event

    result = {row.id: {'merged': False, 'report_status': None, 'selection_reason': None, 'deliveries': []} for row in rows}
    kinds = ('team.member.finished', 'team.member.report', 'team.selection', 'team.delivery')
    latest = select(func.max(Event.id)).where(*scope, Event.kind.in_(kinds)).group_by(Event.session_id, Event.kind)
    for entry in db.scalars(select(Event).where(Event.id.in_(latest))):
        data = payload(entry.kind, entry.payload, redactors[entry.session_id])
        node = result[entry.session_id]
        if entry.kind == 'team.member.report':
            from . import assignments
            attempt = assignments.current(db, entry.session_id)
            if attempt is None or data.get('attempt') == attempt.id:
                node['report_status'] = data.get('status')
        elif entry.kind == 'team.selection':
            node['selection_reason'] = data.get('reason')
        elif entry.kind == 'team.delivery':
            node['deliveries'] = [data]
    # Archiving is a later event, not an undo of an earlier successful merge.
    merged = select(Event.session_id).where(*scope, Event.kind == 'team.member.finished',
                                             Event.payload['merged'].as_boolean().is_(True)).distinct()
    for id in db.scalars(merged):
        result[id]['merged'] = True
    return result
