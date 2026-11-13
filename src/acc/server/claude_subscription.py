"""Personal consent for Claude Agent Chat only; legacy automation guards are unchanged."""
from datetime import datetime, timezone

from .models import AuditLog, Setting, User

VERSION = '2026-11-10.1'
NOTICE = ('Unless previously approved, Anthropic does not allow third party developers to offer '
          'claude.ai login or rate limits for their products, including agents built on the Claude Agent SDK…')
RISK = 'Using my Claude subscription for Chat may put my account at risk. I accept that account risk.'


def key(user_id):
    return f'claude_agent_subscription.{user_id}'


def read(db, user_id):
    row = db.get(Setting, key(user_id))
    person = db.get(User, user_id) if user_id is not None else None
    # SQLite may reuse a deleted numeric ID. Usernames are permanently retired,
    # so another person must neither inherit nor see the former person's consent.
    if row and person and row.value.get('accepted_username') == person.username:
        return dict(row.value)
    return {'enabled': False}


def allowed(db, user_id, features):
    value = read(db, user_id)
    person = db.get(User, user_id) if user_id is not None else None
    return bool(person is not None and value.get('accepted_username') == person.username
                and user_id is not None and features.enabled('claude_subscription_chat') and value.get('enabled')
                and value.get('version') == VERSION and value.get('accepted_by') == user_id)


def update(db, who, enabled, version):
    if who.user_id is None:
        raise ValueError('A signed-in person must accept the risk.')
    if enabled and version != VERSION:
        raise ValueError('Read and accept the current risk notice.')
    value = read(db, who.user_id)
    now = datetime.now(timezone.utc).isoformat()
    if enabled:
        value = dict(enabled=True, accepted_by=who.user_id, accepted_username=who.username,
                     accepted_at=now, version=VERSION, notice=NOTICE, risk=RISK)
    else:
        value = {**value, 'enabled': False, 'revoked_at': now}
    db.merge(Setting(key=key(who.user_id), value=value, updated_by=who.username))
    db.add(AuditLog(actor=who.username, action='claude_subscription.accept' if enabled else 'claude_subscription.revoke',
                    target=str(who.user_id), detail=value))
    return value


def relay_policy(db_factory, features):
    """Broker/hub integration seam. Spec fields must come from the sealed server binding.

    Re-read consent for each mutation so revocation and the owner kill switch take
    effect on running sessions. This never relaxes legacy automation admission.
    """
    def check(spec, operation=None):
        # Turning availability off fences new work, while admitted safety actions
        # remain possible. The hub/broker still checks the caller and run grant.
        if operation and operation.method == 'turn.interrupt':
            return True
        if operation and operation.method == 'decision.reply':
            from acc.session_relay import protocol as p

            from .models import RelayDecision
            from .session_relay import store
            with db_factory() as db:
                identifier = operation.params.get('decisionId')
                record = db.get(RelayDecision, (operation.session_id, identifier)) if identifier else None
                if record is not None:
                    decision = store.decision_from(record.decision)
                    revision = operation.params.get('expectedRevision')
                    pending = decision.status is p.DecisionStatus.PENDING and decision.revision == revision
                    reserved = (decision.status is p.DecisionStatus.RESERVED and type(revision) is int
                                and decision.revision == revision + 1 and decision.winner == operation.actor
                                and decision.choice_id == operation.params.get('choiceId'))
                    if (decision.run_id == operation.run_id and decision.kind is not p.DecisionKind.PRIVILEGED
                            and (pending or reserved)
                            and decision.expires_at_ms > int(datetime.now(timezone.utc).timestamp() * 1000)
                            and operation.params.get('actionDigest') == decision.action_digest
                            and any(choice.id == operation.params.get('choiceId') and choice.effect is p.ChoiceEffect.DENY
                                    for choice in decision.choices)):
                        return True
        if not features.enabled('session_relay'):
            return False
        mode = spec.spec.get('auth_mode')
        if mode in ('api_key', 'cloud'):
            return True
        if mode != 'subscription':
            return False
        from acc.session_relay.protocol import ActorKind
        user_id = spec.spec.get('person_id')
        with db_factory() as db:
            person = db.get(User, user_id) if user_id is not None else None
            if operation and (operation.actor.kind is not ActorKind.PERSON or person is None or
                              operation.actor.principal != person.username):
                return False
            return allowed(db, user_id, features)
    return check
