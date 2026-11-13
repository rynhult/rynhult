"""Current authorization, separate from capability/availability and client identity.

A resolve callback reauthenticates the actual request/device on every call. Never
construct it from client-supplied actor fields or from a cached Principal alone.
"""
from dataclasses import dataclass

from acc.session_relay import protocol as p

from ..models import RelayOwnership, RelayRun, SessionRun, User
from . import store


@dataclass(frozen=True)
class Authority:
    actor: p.Actor
    run_id: int
    epoch: int
    binding_id: str
    policy_revision: int
    delivery_scope: dict | None = None

    def stamp(self):
        return {'actor': p.to_wire(self.actor), 'runId': self.run_id, 'epoch': self.epoch,
                'bindingId': self.binding_id, 'policyRevision': self.policy_revision,
                **({'delivery': self.delivery_scope} if self.delivery_scope is not None else {})}


def current(ctx, db, resolve, session_id, *, action='read', expected_run=None, attached=False, decision=None, generation_checked=False):
    who, actor = resolve.in_transaction(db) if hasattr(resolve, 'in_transaction') else resolve()
    delivery_scope = None
    if actor.kind is p.ActorKind.SERVICE and action != 'read':
        from .delivery import bound
        session, delivery_scope = bound(ctx, db, resolve, session_id, action)
    else:
        session = ctx.session_for(db, who, session_id)
    if action != 'read':
        from .release_scope import local
        local(session, actor)
    if actor.principal != who.username:
        raise p.RelayError(p.ErrorCode.NOT_FOUND_OR_DENIED)
    if actor.kind is p.ActorKind.PERSON:
        user = db.get(User, who.user_id) if who.user_id is not None else None
        if user is None or user.username != who.username or user.role != who.role:
            raise p.RelayError(p.ErrorCode.NOT_FOUND_OR_DENIED)
    elif action != 'read' and delivery_scope is None:
        # Only the exact trusted delivery resolver may carry scoped service mutations.
        raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
    row = store.get(db, session_id, for_update=action != 'read')
    run = db.get(RelayRun, row.current_run_id) if row.current_run_id else None
    if run is None or run.session_id != session_id or run.binding_id != row.binding_id:
        raise p.RelayError(p.ErrorCode.STALE_RUN)
    if expected_run is not None and str(expected_run) != str(row.current_run_id):
        raise p.RelayError(p.ErrorCode.STALE_RUN)
    if action != 'read':
        ownership = db.get(RelayOwnership, session_id)
        from .terminal_grants import NativeResolver
        native = isinstance(resolve, NativeResolver)
        if ownership and action == 'turn.submit' and (ownership.view != ('terminal' if native else 'chat') or
                (ownership.mode == 'exclusive_writer' and row.controller != p.to_wire(actor) and delivery_scope is None)):
            raise p.RelayError(p.ErrorCode.BUSY)
        if ownership and ownership.phase != 'owned' and action not in ('switch', 'decide', 'recover'):
            raise p.RelayError(p.ErrorCode.BUSY)
        if run.identity.get('coordinates') != store.coordinates(session):
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        from .. import security_settings
        try:
            # Launch.check_run has already checked the same session generation
            # in this short transaction. No cached result survives a DB/IO await.
            if not generation_checked:
                security_settings.check_generation(db, session)
        except security_settings.Conflict:
            raise p.RelayError(p.ErrorCode.POLICY_REFUSED) from None
        if run.policy_revision != security_settings.revision(db):
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        native = db.get(SessionRun, row.current_run_id)
        recovering=(action=='recover' and ownership is not None and ownership.mode=='exclusive_idle'
            and ownership.phase in ('recovery_required','recovery_cleaning','recovery_starting')
            and actor.kind is p.ActorKind.PERSON)
        if ((row.sealed and not recovering) or not native or native.ended_at or session.desired_state != 'running'
                or session.team_stop_pending or session.multirun_stop_pending):
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        if action == 'decide':
            # Exact existing person authority only. Group role, control, API and
            # maintainership never create a delegated native approval scope.
            if not attached or actor.kind is not p.ActorKind.PERSON or decision is None:
                raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
            if decision.kind is p.DecisionKind.PRIVILEGED:
                raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
            if not who.is_owner and session.owner != who.username:
                raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
    return row, Authority(actor, row.current_run_id, row.authority_epoch, row.binding_id, run.policy_revision, delivery_scope)


def recheck(previous, current):
    if previous != current:
        raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)


def authenticated_actor(who, *, cookie_authenticated=False, native=False, device=None):
    """Authentication provenance, never a person-shaped service principal, determines kind.

    Internal team/schedule resolvers use the default SERVICE kind even when their
    Principal carries the originating person's user_id. Only the existing auth
    verifier may supply cookie_authenticated=True after checking a human login.
    """
    person = cookie_authenticated is True
    return p.Actor(p.ActorKind.PERSON if person else p.ActorKind.SERVICE, who.username,
                   p.Channel.PHONE if person and native else p.Channel.CHAT if person else p.Channel.API,
                   device if person else None)
