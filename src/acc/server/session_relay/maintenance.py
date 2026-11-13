"""Bounded periodic retention; recovery never launches or resends a provider turn."""
from sqlalchemy import select, tuple_

from acc.session_relay import protocol as p

from ..db import write_intent
from ..models import RelayOwnership, RelaySession
from . import store


async def reconcile_decisions(ctx, *, limit=4):
    """Bounded evidence-only sweep; never launches a model or a writer."""
    from ..broker_client import BrokerError
    from ..models import RelayDecision, Session
    from . import reconciliation
    runtime = getattr(ctx, 'relay_manager', None)
    if not reconciliation.QUALIFIED or runtime is None:
        return
    after = getattr(ctx, '_relay_evidence_after', ('', 0))
    with ctx.db() as db:
        candidates = list(db.execute(select(RelayDecision.session_id, RelayDecision.run_id)
            .join(Session, Session.id == RelayDecision.session_id)
            .where(Session.provider == 'claude', RelayDecision.decision['choiceId'].as_string() == 'accept',
                tuple_(RelayDecision.session_id, RelayDecision.run_id) > after,
                RelayDecision.decision['status'].as_string().in_(['reserved','outcome_unknown']))
            .distinct().order_by(RelayDecision.session_id, RelayDecision.run_id).limit(limit)))
    selected = candidates
    for session_id, run_id in selected:
        ctx._relay_evidence_after = (session_id, run_id)
        try:
            await runtime.reconcile_decisions(session_id, run_id)
        except (OSError, ConnectionError, TimeoutError, BrokerError, p.RelayError):
            pass  # unresolved evidence stays unknown; no replay or owner recovery
    if len(selected) < limit:
        ctx._relay_evidence_after = ('', 0)


@write_intent
def maintain(ctx, *, limit=16):
    # Seek round-robin so an old large enrollment cannot starve later sessions.
    after = getattr(ctx, '_relay_retention_after', '')
    with ctx.db() as db:
        ids = list(db.scalars(select(RelaySession.session_id).where(RelaySession.session_id > after)
                             .order_by(RelaySession.session_id).limit(limit)))
    for identifier in ids:
        with ctx.db() as db:
            row = db.get(RelaySession, identifier)
            if row is not None:
                store.prune(db, store.lock(db, identifier))
                db.commit()
        ctx._relay_retention_after = identifier
    if len(ids) < limit:
        ctx._relay_retention_after = ''


@write_intent
def recover(ctx, *, limit=16):
    """Fence a bounded batch once per coordinator lifetime, without replay."""
    if getattr(ctx, '_relay_recovery_done', False):
        return
    after = getattr(ctx, '_relay_recovery_after', '')
    with ctx.db() as db:
        ids = list(db.scalars(select(RelaySession.session_id).where(RelaySession.session_id > after)
                             .order_by(RelaySession.session_id).limit(limit)))
    for identifier in ids:
        with ctx.db() as db:
            runtime = getattr(ctx, 'relay_manager', None)
            if runtime is not None and runtime.is_current_launch(identifier):
                continue
            if db.get(RelaySession, identifier):
                row = store.lock(db, identifier)
                owner = db.get(RelayOwnership, identifier)
                if owner:
                    owner.phase = 'recovery_required'
                    row.capabilities = {}
                    store.seal(db, row)
                else:
                    store.recover(db, row)
                db.commit()
        ctx._relay_recovery_after = identifier
    if len(ids) < limit:
        ctx._relay_recovery_done = True
