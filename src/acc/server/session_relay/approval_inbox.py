"""Display-only native review facts. Answers remain authenticated hub operations."""
from __future__ import annotations

from sqlalchemy import func, select

from acc.session_relay.approval_facts import complete

from .. import approval_cards, attention
from ..admin_features import enabled
from ..models import Event, RelayDecision, RelaySession

PREFIX = 'relay_decision:'


def publish(db, row, decision_id, payload, mask):
    # Bound before redaction; over-limit input is never partially presented as complete.
    rendered = payload.get('acc_complete_input')
    if not complete(payload):
        rendered = None
    safe = {'acc_complete_input': mask(rendered) if rendered is not None else None,
            'tool_name': mask(str(payload.get('tool_name', ''))[:200]),
            'cwd': mask(payload['cwd']) if isinstance(payload.get('cwd'), str) and len(payload['cwd'].encode()) <= 2000 else None}
    facts = approval_cards.validated_card(approval_cards.card(safe))
    db.add(Event(session_id=row.session_id, source='session_relay', kind='RelayApproval', verified=True,
        payload={'decisionId': decision_id, 'runId': str(row.current_run_id),
                 'approval_card': facts, 'permission_input': safe['acc_complete_input']}))
    attention.raise_item(db, PREFIX + row.session_id + ':' + decision_id, kind='needs_input',
        title='Review native tool action', source='session_relay', session_id=row.session_id,
        detail='Review the native request. Input ownership does not grant approval authority.')


def projections(ctx, db, items, authorized_sessions):
    """Only authorized listed sessions; exact decision/run join, never latest-by-session."""
    selected = {item.session_id for item in items if item.session_id in authorized_sessions
                and item.dedupe_key.startswith(PREFIX)}
    if not selected:
        return {}
    identifiers = {item.dedupe_key.removeprefix(PREFIX + item.session_id + ':') for item in items
                   if item.session_id in selected and item.dedupe_key.startswith(PREFIX)}
    latest = select(func.max(Event.id)).where(Event.session_id.in_(selected), Event.source == 'session_relay',
        Event.kind == 'RelayApproval', Event.payload['decisionId'].as_string().in_(identifiers)).group_by(
            Event.session_id, Event.payload['decisionId'].as_string())
    events = db.scalars(select(Event).where(Event.id.in_(latest)))
    facts = {}
    for event in events:
        value = event.payload or {}
        facts.setdefault((event.session_id, value.get('decisionId')), value)
    result = {}
    from . import claude_public, history
    for item in items:
        if item.session_id not in selected or not item.dedupe_key.startswith(PREFIX):
            continue
        identifier = item.dedupe_key.removeprefix(PREFIX + item.session_id + ':')
        record = db.get(RelayDecision, (item.session_id, identifier))
        row = db.get(RelaySession, item.session_id)
        value = facts.get((item.session_id, identifier))
        if not record or not row or not value or value.get('runId') != str(record.run_id):
            continue
        result[item.id] = {'approval_card': approval_cards.validated_card(value.get('approval_card')),
            'permission_input': value.get('permission_input'), 'relay_decision': history.public_actors(record.decision),
            'relay_allow_enabled': enabled(ctx.settings, 'session_relay'),
            'relay_decision_available': record.run_id == row.current_run_id and not row.sealed
                and claude_public.decisions_ready(ctx, row)}
    return result
