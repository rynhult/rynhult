"""Durable fact of one authenticated provider enclosure's conclusive Stop.

This is never a launch/input/recovery grant. Absence of a live runtime is not
proof. Only an exact exited response may create this bounded per-run record.
"""
from sqlalchemy import select

from acc.session_relay import protocol as p

from ..db import write_intent
from ..models import Node, RelayRun, Session, SessionRun, Team, utcnow
from . import store

KEY = 'conclusiveStop'


def stamp(row, record):
    # Application origins are mutable receipt facts, never producer identity.
    identity = {key: value for key, value in record.identity.items()
                if key not in (KEY, 'applicationOrigins')}
    return {'version': 1, 'enrollment': row.enrollment, 'runId': record.run_id,
            'bindingId': record.binding_id, 'epoch': row.authority_epoch,
            'identityDigest': p.sha256_digest(p.canonical_json(identity))}


def placement(manager, db, session):
    # Stop/exit facts must remain usable after admission policy/budget loss.
    # Verify physical node/account/leader identity without asking for new input.
    broker = manager.ctx.broker
    leader = getattr(broker, 'authority', None)
    if leader:
        leader.check()
    host = session.host or 'local'
    if host != 'local':
        node = db.scalar(select(Node).where(Node.name == host))
        if node is None or node.status != 'active':
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        link = broker.hub.get(host)
        if link.closed or session.run_as not in link.accounts:
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        node_identity = [node.id, node.public_key, id(link)]
    else:
        if leader:
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        node_identity = ['local']
    if (session.container or session.management != 'acc'
            or session.multirun_id or session.automation):
        raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
    team = None
    if session.team_id:
        record = db.get(Team, session.team_id)
        if record is None:
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        # Match the exact G4 placement binding, without requiring permission to
        # admit more team work. Stopped/expired teams can still have exited trees.
        team = {'id': record.id, 'limits': record.limits,
                'expires': record.expires_at.isoformat() if record.expires_at else None}
    return [host, session.run_as, node_identity, getattr(leader, 'epoch', None),
            getattr(leader, 'incarnation', None), session.run_dir,
            *([{'team': team}] if team is not None else [])]


def current(manager, db, identifier):
    row = store.lock(db, identifier)
    session = db.get(Session, identifier)
    record = db.get(RelayRun, row.current_run_id) if row.current_run_id else None
    native = db.get(SessionRun, row.current_run_id) if row.current_run_id else None
    if (not session or session.provider not in ('claude', 'codex') or not row.sealed or not record or not native
            or record.identity.get('binding', {}).get('provider') != session.provider
            or record.session_id != identifier or native.session_id != identifier
            or record.binding_id != row.binding_id or native.verified_at is None
            or record.identity.get('coordinates') != store.coordinates(session)
            or record.identity.get('placement') != placement(manager, db, session)):
        raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
    return row, session, record, native


@write_intent
def remember(manager, run):
    with manager.ctx.db() as db:
        row, _, record, native = current(manager, db, run.session_id)
        if (record.run_id != run.run_id or record.binding_id != run.binding_id
                or record.identity.get('brokerIncarnation') != run.fence.broker_incarnation
                or record.identity.get('hubIncarnation') != run.fence.hub_incarnation):
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        record.identity = {**record.identity, KEY: stamp(row, record)}
        native.ended_at = native.ended_at or utcnow()
        db.commit()
    return True


@write_intent
def verified(manager, identifier):
    try:
        with manager.ctx.db() as db:
            row, session, record, native = current(manager, db, identifier)
            return (session.desired_state in ('stopped', 'archived') and native.ended_at is not None
                    and p.canonical_json(record.identity.get(KEY)) == p.canonical_json(stamp(row, record)))
    except (p.RelayError, KeyError, TypeError, ValueError):
        return False
