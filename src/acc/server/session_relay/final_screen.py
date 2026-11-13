"""Bounded retained native view, authorized afresh without a runtime or grant."""
from sqlalchemy import select

from acc.session_relay import protocol as p

from ..models import RelayEvent, RelayRun, Session, SessionRun
from . import authority, history

MAX_SCREEN_BYTES = 120 << 10  # leave event/frame metadata and redaction headroom


def sanitize(value, mask):
    if value is None:
        return None
    if (not isinstance(value, dict) or value.keys() != {'text', 'truncated'}
            or not isinstance(value['text'], str) or type(value['truncated']) is not bool
            or len(value['text'].encode()) > 128 << 10):
        raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
    safe = history.safe_display(value, mask)
    if len(p.canonical_json(safe)) > MAX_SCREEN_BYTES:
        return None  # never retain a partial-line secret or silently omit a draft
    return safe


def read(ctx, resolve, session_id, expected_run):
    if not expected_run.isascii() or not expected_run.isdecimal() or len(expected_run) > 19:
        raise p.RelayError(p.ErrorCode.NOT_FOUND_OR_DENIED)
    identifier = int(expected_run)
    if not 0 < identifier < (1 << 63) or str(identifier) != expected_run:
        raise p.RelayError(p.ErrorCode.NOT_FOUND_OR_DENIED)
    with ctx.db() as db:
        row, current = authority.current(ctx, db, resolve, session_id)
        run, native = db.get(RelayRun, identifier), db.get(SessionRun, identifier)
        if run and native and run.session_id == session_id and native.session_id == session_id and row.sealed and native.ended_at is None:
            from .release_scope import refuse
            refuse()
        if (not run or not native or run.session_id != session_id or native.session_id != session_id
                or native.ended_at is None):
            raise p.RelayError(p.ErrorCode.NOT_FOUND_OR_DENIED)
        events = db.scalars(select(RelayEvent).where(RelayEvent.session_id == session_id,
            RelayEvent.event['runId'].as_string() == expected_run,
            RelayEvent.event['kind'].as_string() == 'session.stateChanged')
            .order_by(RelayEvent.seq.desc()).limit(32)).all()
        final = next((event.event['payload'] for event in events if 'finalScreen' in event.event.get('payload', {})), None)
        if final is None or final['finalScreen'] is None:
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        session = db.get(Session, session_id)
        mask = ctx.supervisor.vault.redactor(db, session.vault_keys or []).text
        result = sanitize(final['finalScreen'], mask)
        if result is None:
            raise p.RelayError(p.ErrorCode.LIMIT)
        stamp = (row.enrollment, row.history_epoch, current)
        return stamp, {'runId': expected_run, **result}
