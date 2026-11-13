"""Bounded safe replay. Cursors bind the viewer, enrollment, run and history epoch."""
import base64
import hashlib
import hmac
import json
import time

from sqlalchemy import func, select

from acc.session_relay import protocol as p

from ..models import RelayDecision, RelayEvent, RelayOperation, RelayRun, SessionRun
from . import store


class History:
    def __init__(self, secret):
        if len(secret) < 32:
            raise ValueError('cursor key must have at least 256 bits')
        self.secret = secret

    def cursor(self, row, scope, seq):
        value = [1, scope, row.session_id, row.enrollment, row.current_run_id, row.history_epoch, seq, int(time.time()) + 3600]
        raw = p.canonical_json(value)
        return base64.urlsafe_b64encode(raw).decode().rstrip('=') + '.' + hmac.new(self.secret, raw, 'sha256').hexdigest()

    def decode(self, token, row, scope):
        try:
            if not isinstance(token, str) or len(token) > 2048:
                raise ValueError()
            body, signature = token.split('.')
            raw = base64.b64decode(body + '=' * (-len(body) % 4), altchars=b'-_', validate=True)
            if not hmac.compare_digest(signature, hmac.new(self.secret, raw, 'sha256').hexdigest()):
                raise ValueError()
            value = json.loads(raw)
            if (len(value) != 8 or value[:6] != [1, scope, row.session_id, row.enrollment, row.current_run_id, row.history_epoch]
                    or type(value[6]) is not int or not row.floor_seq <= value[6] <= row.event_seq
                    or type(value[7]) is not int or value[7] <= time.time()):
                raise ValueError()
            return value[6]
        except (ValueError, TypeError, KeyError):
            raise p.RelayError(p.ErrorCode.CURSOR_RESET) from None

    def page(self, db, row, scope, *, cursor=None, limit=p.HISTORY_DEFAULT_ITEMS, actor, mask, run_id=None):
        if type(limit) is not int or not 1 <= limit <= p.HISTORY_MAX_ITEMS:
            raise p.RelayError(p.ErrorCode.LIMIT)
        filters = [RelayEvent.session_id == row.session_id]
        watermark = row.event_seq
        if run_id is not None:
            if type(run_id) is not int or not 0 < run_id < (1 << 63):
                raise p.RelayError(p.ErrorCode.NOT_FOUND_OR_DENIED)
            run = db.get(RelayRun, run_id)
            if run is None or run.session_id != row.session_id:
                raise p.RelayError(p.ErrorCode.NOT_FOUND_OR_DENIED)
            # Past-run selection is read-only and scoped to the currently
            # authorized enrollment. Its cursor cannot select another run.
            scope = hashlib.sha256(p.canonical_json([scope, str(run_id)])).hexdigest()
            filters.append(RelayEvent.event['runId'].as_string() == str(run_id))
            watermark = max(row.floor_seq, db.scalar(select(func.max(RelayEvent.seq)).where(*filters)) or 0)
        after = self.decode(cursor, row, scope) if cursor else row.floor_seq
        items, size, position = [], 0, after
        events = db.scalars(select(RelayEvent).where(*filters, RelayEvent.seq > after)
                            .order_by(RelayEvent.seq).limit(limit + 1)).all()
        for event in events[:limit]:
            # Receipt payloads are actor-private, just like operation.get. Advance
            # the watermark over invisible events without exposing foreign metadata.
            payload = event.event.get('payload', {})
            receipt = payload.get('receipt')
            if receipt and store.actor_key(store.actor_from(receipt['actor'])) != store.actor_key(actor):
                position = event.seq
                continue
            safe = safe_display(public_actors(event.event), mask)
            count = len(p.encode_frame(safe))
            if size + count > p.HISTORY_MAX_BYTES:
                if not items:
                    # Fresh redaction/JSON escaping can expand one valid stored
                    # event beyond a page. Never return an empty successful page
                    # whose cursor cannot advance, or skip/truncate that event.
                    raise p.RelayError(p.ErrorCode.LIMIT)
                break
            size += count
            items.append(safe)
            position = event.seq
        return {'events': items, 'cursor': self.cursor(row, scope, position), 'watermark': watermark,
                'historyEpoch': row.history_epoch, 'gap': cursor is None and row.floor_seq > 0,
                'more': position < watermark}


def snapshot(db, row, actor):
    decisions = tuple(store.decision_from(r.decision) for r in db.scalars(select(RelayDecision)
                      .where(RelayDecision.session_id == row.session_id, RelayDecision.run_id == row.current_run_id))
                      if r.decision['status'] in ('pending', 'reserved', 'outcome_unknown'))
    # Bounded recent receipts; complete retained lookup remains available by operation ID.
    operations = db.scalars(select(RelayOperation).where(RelayOperation.session_id == row.session_id,
                            RelayOperation.actor_key == store.actor_key(actor))
                            .order_by(RelayOperation.created_at.desc(), RelayOperation.id.desc()).limit(100)).all()
    from ..models import RelayOwnership
    owner = db.get(RelayOwnership, row.session_id)
    writer = None
    if owner and owner.mode == 'exclusive_writer':
        writer = {'view': owner.view, 'holder': public_actors(row.controller),
                  'revision': row.control_revision, 'active': not row.sealed, 'available': False,
                  'mine': row.controller == p.to_wire(actor)}
    return p.Snapshot(row.session_id, str(row.current_run_id) if row.current_run_id else None,
                      row.history_epoch, row.event_seq, row.control_revision, row.configuration_revision,
                      {key: p.Support(value) for key, value in row.capabilities.items()}, p.Support.UNSUPPORTED, decisions, tuple(store.receipt_from(op.receipt) for op in operations),
                      store.actor_from(row.controller) if row.controller else None, writer)


def message(db, row, *, item_id, role, complete_text, source_epoch, source_revision, mask):
    """Ingress for a complete P3a-normalized block, never an incomplete token delta.

    Existing native readers retain ownership of source verification/normalization.
    No transcript is fetched or provider started here. The caller supplies its
    scoped vault redactor before durable storage, then readers redact again.
    """
    if role not in ('user', 'assistant') or not p.valid_opaque(item_id) or len(complete_text.encode()) > 128 << 10:
        raise p.RelayError(p.ErrorCode.LIMIT)
    source_key = hashlib.sha256(p.canonical_json([row.current_run_id, source_epoch, item_id, source_revision])).hexdigest()
    return store.append(db, row, 'message.upsert', {'itemId': item_id, 'role': role, 'text': mask(complete_text)},
                        source_key=source_key)


def reset(db, row):
    row.history_epoch += 1
    store.append(db, row, 'history.reset', {'historyEpoch': row.history_epoch})


def safe_display(value, mask):
    if isinstance(value, dict):
        return {key: mask(child) if key in ('text', 'description', 'label') and isinstance(child, str)
                else safe_display(child, mask) for key, child in value.items()}
    if isinstance(value, list):
        return [safe_display(child, mask) for child in value]
    return value


def public_actors(value):
    """Device bindings are authority material, never multi-viewer presentation."""
    if isinstance(value, dict):
        actor = {'kind', 'principal', 'channel'} <= value.keys()
        return {key: public_actors(child) for key, child in value.items() if not (actor and key == 'device')}
    if isinstance(value, list):
        return [public_actors(child) for child in value]
    return value


def final_events_drained(db, row):
    """A bounded received-event drain fact, never full native-history capability."""
    events = db.scalars(select(RelayEvent).where(RelayEvent.session_id == row.session_id,
        RelayEvent.event['runId'].as_string() == str(row.current_run_id),
        RelayEvent.event['kind'].as_string() == 'session.stateChanged')
        .order_by(RelayEvent.seq.desc()).limit(32)).all()
    final = next((event.event['payload'] for event in events if 'exitOutcome' in event.event.get('payload', {})), {})
    return final.get('finalEventsDrained') is True


def retained_runs(db, row):
    """Recent retained run choices; no native identity, execution or grant."""
    records = db.execute(select(RelayRun, SessionRun).join(SessionRun, SessionRun.id == RelayRun.run_id)
        .where(RelayRun.session_id == row.session_id, SessionRun.session_id == row.session_id,
            (SessionRun.ended_at.is_not(None)) | (RelayRun.run_id != row.current_run_id) | row.sealed)
        .order_by(RelayRun.run_id.desc()).limit(51)).all()
    outcomes = {'native_exit_normal': 'normal', 'native_exit_abnormal': 'abnormal'}
    return {'currentRunId': str(row.current_run_id), 'runs': [
        {'runId': str(run.run_id), 'exitOutcome': outcomes.get(native.end_reason, 'unknown'),
         'endedAt': native.ended_at.isoformat() if native.ended_at else None}
        for run, native in records[:50]], 'more': len(records) > 50}
