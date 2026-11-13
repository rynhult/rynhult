"""Current per-session authority, always resolved through immutable lineage."""
from __future__ import annotations

import json

from ..session_capabilities import Refused, launch, meet, normalize, require
from .models import AuditLog, Team

FILE = 'session-capabilities.json'


def effective(db, session, *, running=False):
    from .child_caps import chain
    result = normalize(getattr(session, 'cap_requested', None))
    for row in [session, *chain(db, session)]:
        result = meet(result, getattr(row, 'cap_requested', None))
        result = meet(result, getattr(row, 'cap_limit', None))
        if getattr(row, 'team_id', None):
            team = db.get(Team, row.team_id)
            if team and team.expires_at:
                deadline = team.expires_at
                # Only an already-running lead may finish the bounded expiry
                # wrap-up. Launch and operational authority retain the expiry.
                if (running and row is session and not session.parent_session_id
                        and team.id == session.id and session.desired_state == 'running'):
                    if team.stopped and team.stop_reason == 'expired' and team.wrapup_until:
                        deadline = team.wrapup_until
                    elif not team.stopped:
                        from .models import utcnow
                        if team.expires_at <= utcnow():
                            # Reconciliation can see expiry before the lifetime pass
                            # records it. Use the same original-expiry-bound window;
                            # never extend launch/operation authority or child expiry.
                            from .orchestrator.verification import deadline as wrapup_deadline
                            deadline = wrapup_deadline(db, team)
                result = meet(result, {'expires_at': int(deadline.timestamp())})
    return result


def origin(db, session, dimension, value=None):
    from .child_caps import chain
    for parent in chain(db, session):
        try:
            require(effective(db, parent), dimension, value)
        except Refused:
            return parent.id
    return None


def authorize(db, session, dimension, value=None):
    # Compute the live intersection before the frozen-generation check so a
    # denied operation names the ancestor that actually removed the authority.
    caps = effective(db, session)
    require(caps, dimension, value, parent=origin(db, session, dimension, value))
    from .child_caps import check
    check(db, session)
    return caps


def admit_launch(db, session, *, running=False):
    from .child_caps import check
    check(db, session)
    caps = effective(db, session, running=running)
    parent = session.parent_session_id
    inputs = session.launch_spec.inputs
    launch(caps, session.provider, inputs.get('model'), container=session.container, parent=parent)
    return caps


def attach(db, session, requested=None):
    session.cap_requested = normalize(requested)
    session.cap_limit = normalize()
    session.cap_effective = effective(db, session)
    return json.dumps({'requested': session.cap_requested, 'effective': session.cap_effective,
                       'parent': session.parent_session_id, 'model': session.launch_spec.inputs.get('model')}, sort_keys=True)


def narrow(db, session, proposed, actor):
    updates = normalize(proposed)
    proposed = normalize({**effective(db, session), **proposed})
    # This operation is narrowing only, including against an inherited ceiling.
    before = effective(db, session)
    after = meet(before, proposed)
    if after != proposed:
        raise Refused('this endpoint can only narrow the current effective grant')
    if after == before:
        return after
    session.cap_limit = meet(session.cap_limit, updates)
    db.add(AuditLog(actor=actor, action='session.permissions.narrow', target=session.id,
                    detail={'before': before, 'after': after}))
    return after


def check_execution(db, session, wrapup=False):
    caps = admit_launch(db, session, running=wrapup)
    applied = session.cap_effective
    if applied is not None and any(caps[key] != applied.get(key) for key in ('folders', 'network', 'installs', 'ports', 'providers', 'models')):
        raise Refused(f'limited by {session.parent_session_id or session.id}: running sandbox grant changed; resume with the narrowed grant')
    return caps


def wire(db, session):
    caps = admit_launch(db, session)
    return {'requested': normalize(session.cap_requested), 'effective': caps, 'parent': session.parent_session_id, 'model': session.launch_spec.inputs.get('model')}


def attribution(session):
    if session is None:
        return {'parent_chain': [], 'root_owner': None}
    return {'parent_chain': [*(session.parent_chain or []), session.id] if session.parent_session_id else [],
            'root_owner': session.root_owner or session.owner}
