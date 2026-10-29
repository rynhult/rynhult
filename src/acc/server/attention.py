"""Attention items: raise (deduplicated by key) and resolve. Callers pass verified signals only."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session as DbSession

from .. import providers
from .models import AttentionItem, Observation, Session, User

LOGIN_WARN_DAYS = 7
LOGIN_CRITICAL_DAYS = 2

PERSISTENT_ALERTS = {
    "machine_offline:": "machine_offline",
    "backup:failed:": "backup_failed",
    "backup:stale:": "backup_stale",
}


def persistent_reason(item: AttentionItem) -> str | None:
    return next((reason for prefix, reason in PERSISTENT_ALERTS.items() if item.dedupe_key.startswith(prefix)), None)


def persistent_condition():
    return or_(*(AttentionItem.dedupe_key.startswith(prefix, autoescape=True) for prefix in PERSISTENT_ALERTS))


def now() -> datetime:
    return datetime.now(timezone.utc)


def _open(db: DbSession, key: str) -> AttentionItem | None:
    return db.scalar(select(AttentionItem).where(AttentionItem.dedupe_key == key, AttentionItem.resolved_at.is_(None)))


def raise_item(db: DbSession, key: str, *, kind: str, title: str, source: str, severity: str = "warn",
               detail: str | None = None, session_id: str | None = None, verified: bool = True,
               owner: str | None = None, risk: dict | None = None) -> AttentionItem:
    """`owner` (a username) sees it besides the owners; by default the session's owner, else owners only."""
    item = _open(db, key)
    if item is None:
        if owner is None and session_id is not None and (session := db.get(Session, session_id)) is not None:
            owner = session.owner
        if owner and db.scalar(select(User.id).where(User.username == owner)) is None:
            owner = None  # e.g. "@api" (sessions started with the API token): nobody signs in as it, so owners see it
        owner = owner or None  # "": explicitly owners only
        item = AttentionItem(dedupe_key=key, kind=kind, title=title, source=source, severity=severity,
                             detail=detail, session_id=session_id, verified=verified, owner=owner, risk=risk)
        db.add(item)
    else:
        if severity == "critical" and item.severity != "critical":
            item.acked_at = None  # escalation reopens it
        item.title, item.detail, item.severity, item.updated_at = title, detail, severity, now()
        item.risk = risk
        if owner is not None:
            item.owner = owner or None  # e.g. the member an account belongs to signed up meanwhile
    return item


def resolve(db: DbSession, key: str) -> None:
    item = _open(db, key)
    if item is not None:
        item.resolved_at = now()


def account_user(db: DbSession, account: str) -> str | None:
    """The member whose agent account this is (owners see every item anyway)."""
    return db.scalar(select(User.username).where(User.account == account, User.role == "member").limit(1))


def evaluate_auth(db: DbSession, health: dict, agent_user: str = "the agent user", key_suffix: str = "") -> None:
    """Login expiry and logged-out states for every provider's CLI and GitHub (plan v1.3), for one agent account
    (`key_suffix` keeps other accounts' items apart; the default account has none). A member's account's items are
    theirs too."""
    names = [*providers.ids(), "github"]
    owner = account_user(db, agent_user) if key_suffix else None
    if key_suffix and owner is None:  # nobody uses this account yet: nobody to tell, and owners don't want its alerts
        for provider in names:
            resolve(db, f"auth:{provider}{key_suffix}")
        return
    # Only a provider this account has used can be "logged out": one never set up (e.g. a member without OpenAI) raises
    # nothing. Providers seen logged in are remembered per account.
    seen_key = f"auth.seen.{agent_user}"
    seen_row = db.get(Observation, seen_key)
    seen = set((seen_row.value or {}).get("providers", [])) if seen_row else set()
    now_in = {name for name in names if (health.get(name) or {}).get("logged_in")}
    if not now_in <= seen:
        seen |= now_in
        db.merge(Observation(key=seen_key, value={"providers": sorted(seen)}, source="auth", observed_at=now().timestamp()))
    for name in names:
        key = f"auth:{name}{key_suffix}"
        if name not in seen:
            resolve(db, key)
            continue
        status = health.get(name) or {}
        if name == "github":
            label, severity, source = "GitHub", "warn", "gh auth status"
            detail = "Agents can't push or open PRs. Use Log in on the Providers page."
        else:
            spec = providers.get(name)
            label, severity, source = spec.label, "critical", f"{name} login status"
            detail = "Sessions cannot reach Claude. Use Re-login on the Providers page." if name == "claude" else \
                "Use Re-login on the Providers page."
        if not status.get("logged_in"):
            ready_only = name != "github" and providers.get(name).login is None  # no sign-in of its own (OpenCode)
            raise_item(db, owner=owner, key=key, kind="auth", severity=severity, source=source,
                       title=f"{label} isn't responding for {agent_user}" if ready_only
                       else f"{label} is logged out for {agent_user}",
                       detail=f"{status.get('error') or 'Its status check failed'}. See the Providers page." if ready_only
                       else detail)
            continue
        expires = status.get("login_expires_at")
        days = (expires - now().timestamp()) / 86400 if expires else None
        if days is not None and days < LOGIN_WARN_DAYS:
            short = label.split()[0]  # "Claude login for …"
            raise_item(db, owner=owner, key=key, kind="auth", source="credentials expiry",
                       severity="critical" if days < LOGIN_CRITICAL_DAYS else "warn",
                       title=f"{short} login for {agent_user} expires in {max(days, 0):.1f} days",
                       detail="Re-login from the Providers page before it lapses; running sessions keep their conversations.")
        else:
            resolve(db, key)


# A positive list keeps new decision kinds out of bulk dismissal by default.
_BULK_NOTICES = frozenset({
    "state", "guardrail_blocked", "crash_recovered", "stop_failure", "auth",
    "publish_failed", "tool_failed", "spool_parse_error", "ci_failed", "linear_comment",
    "launch_failed", "schedule_failed", "git_identity_warning", "worktree_kept",
    "worktree_removed", "remote_spool_full", "budget", "update", "compose", "host",
    "backup", "git_backup_kept",
})


def bulk_dismissible(item: AttentionItem) -> bool:
    return (item.kind in _BULK_NOTICES and not persistent_reason(item)
            and item.acked_at is None and item.resolved_at is None
            and (item.snoozed_until is None or item.snoozed_until <= now()))
