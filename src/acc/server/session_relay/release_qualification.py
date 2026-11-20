"""Owner-only Claude/Codex relay preview grant (docs/decisions/0074-relay-preview.md).

The only writer of non-empty relay capabilities. Frozen declarations qualify
Chat/Terminal for Codex and also the two L1 switches for Claude's pinned CLI/SDK,
only for an install owner's current unsealed run. Any failed condition writes {}.
Recovery and fence-loss paths never call this.

The grant compares the run's recorded probe digest with its declaration; that
is a consistency check only. Launch already refuses a mismatched probe, and the
real CLI guard is the broker's own executable pin when it starts the owner
(acc.broker.claude_owner / relay_runtime). Later person-preview mutations re-check the owner
through OwnershipHub._policy for Claude and authority.current for Codex.
"""
from __future__ import annotations

from types import MappingProxyType

from sqlalchemy import select

from acc.broker.claude_owner import CLI_DIGEST
from acc.session_relay import protocol as p
from acc.session_relay.codex_preview import DECLARATION as CODEX_DECLARATION

from .. import claude_subscription
from ..admin_features import Features, enabled
from ..models import RelaySession, Session, SessionRun, User

QUALIFICATION_ID = 'rynhult-relay-owner-preview-2026-11-13'
SDK_VERSION = '0.1.50'
EVIDENCE = ('docs/decisions/0074-relay-preview.md',
            'docs/handoffs/2026-11-12-relay-r1-group10-public-native-cas-20.json')
OPERATIONS = ('text', 'terminal.input', 'switch.to_chat', 'switch.to_terminal')
OWNER_ONLY = 'The session relay preview is limited to install owners.'

DECLARATION = p.CapabilityDeclaration(
    'claude', '2.1.308', CLI_DIGEST, f'claude-agent-sdk/{SDK_VERSION}', f'sdk-stdio-{SDK_VERSION}', 'stdio',
    QUALIFICATION_ID, '2026-11-13', MappingProxyType({'placement': 'local-or-node', 'actor': 'install-owner'}),
    MappingProxyType({name: p.CapabilityEntry(p.Support.QUALIFIED, 'Owner preview.', evidence=EVIDENCE)
                      for name in OPERATIONS}))


def flags(provider='claude'):
    from . import claude_public, exclusive_public, writers
    if provider=='codex':
        return writers.qualified('codex')
    if provider!='claude':return False
    return (exclusive_public.CLAUDE_LAUNCH_QUALIFIED is True and claude_public.PUBLIC_CLAUDE_QUALIFIED is True
            and writers.PUBLIC_WRITER_QUALIFIED is True)


def owner(db, actor):
    """The install-owner User for a person actor, else None."""
    if actor is None or actor.kind is not p.ActorKind.PERSON:
        return None
    user = db.scalar(select(User).where(User.username == actor.principal))
    return user if user is not None and user.role == 'owner' else None


def allowed(ctx, db, row, run, actor):
    """Every preview condition, checked in the caller's transaction.

    run.identity['cliDigest'] is the digest an available broker relay_probe
    reported at launch; it is compared, not re-measured, here.
    """
    session = db.get(Session, row.session_id)
    native = db.get(SessionRun, run.run_id) if run is not None else None
    declaration = (CODEX_DECLARATION if session is not None and session.provider=='codex' else DECLARATION)
    if (session is None or run is None or session.provider not in ('claude','codex') or not flags(session.provider)
            or not enabled(ctx.settings, 'session_relay')
            or row.sealed or row.current_run_id != run.run_id or row.binding_id != run.binding_id
            or native is None or native.ended_at is not None
            or (run.identity or {}).get('cliDigest') != declaration.cli_digest):
        return False
    user = owner(db, actor)
    if user is None or session.owner != actor.principal:
        return False
    from .release_scope import local
    try:
        local(session, actor)
    except p.RelayError:
        return False
    if session.provider=='claude' and (run.frozen_policy or {}).get('authMode') == 'subscription' and (
            user.id != run.frozen_policy.get('personId')
            or not claude_subscription.allowed(db, user.id, Features(ctx.settings))):
        return False
    return True


def grant(ctx, db, row, run, actor):
    """Write the effective preview capabilities, or {} when any condition fails."""
    if not allowed(ctx, db, row, run, actor):
        row.capabilities = {}
        return row.capabilities
    declaration = CODEX_DECLARATION if db.get(Session,row.session_id).provider=='codex' else DECLARATION
    live = set(declaration.operations)
    effective = p.effective_capabilities(declaration, live=live, placement=live, actor=live)
    row.capabilities = {name: support.value for name, support in sorted(effective.items())
                        if support is p.Support.QUALIFIED}
    return row.capabilities


def require_owner(db, session, actor):
    """Every Claude/Codex person-preview mutation: the session's own install-owner person.

    A refusal also withdraws the preview grant in this transaction, so a demoted
    owner or another owner keeps nothing until a fresh launch or switch grant.
    """
    if session is None or session.provider not in ('claude','codex'):
        return
    if actor is not None and owner(db, actor) is not None and session.owner == actor.principal:
        return
    row = db.get(RelaySession, session.id)
    if row is not None and row.capabilities:
        row.capabilities = {}
        db.commit()
    raise p.RelayError(p.ErrorCode.POLICY_REFUSED, details={'reason': 'owner_only_preview'})
