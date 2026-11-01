from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

# Who owns what the bootstrap bearer token creates (projects, sessions, keys): never a username, since usernames start
# with a letter or digit, so nobody can sign up as it and inherit those rows.
API_PRINCIPAL = "@api"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """SQLite drops tzinfo; store UTC and hand back aware datetimes."""
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value is not None else None

    def process_result_value(self, value, dialect):
        return value.replace(tzinfo=timezone.utc) if value is not None else None


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "project"
    __table_args__ = (CheckConstraint("group_id IS NULL OR owner IS NULL", name="project_single_authority"),
                      {"sqlite_autoincrement": True})
    group_id: Mapped[str | None] = mapped_column(ForeignKey("acc_group.id"), index=True)
    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    host: Mapped[str] = mapped_column(String(64), default="local")  # the machine it lives on (M11: more hosts)
    workspace_path: Mapped[str] = mapped_column(Text)
    repo: Mapped[str | None] = mapped_column(String(200))
    remote_url: Mapped[str | None] = mapped_column(Text)
    issue_sources: Mapped[dict | None] = mapped_column(JSON)
    import_mode: Mapped[str] = mapped_column(String(16), default="managed")  # managed | readonly
    deleting: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    managed_checkout: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    trusted: Mapped[bool] = mapped_column(Boolean, default=False)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)
    # Project runtime (M5): bring the compose stack up whenever accd starts (e.g. after a reboot).
    compose_autostart: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # The compose files an owner approved (D4: agents can edit them, the stack runs as the owner): {digest, files,
    # at, by}. Autostart runs the stack only while its files still match (acc.server.compose).
    compose_pin: Mapped[dict | None] = mapped_column(JSON)
    # Git backup to the main ACC (M15): on unless turned off on the project page.
    backup_identity: Mapped[str | None] = mapped_column(String(120))  # stable across private workspace promotion
    private_workspace: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    git_backup: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    # Per-session containers (docs/CONTAINER-LAUNCHER.md): an owner's choice, off by default; new sessions in the
    # project then run in a container through the acc-containers root helper (only while ACC_CONTAINERS is on).
    containers: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # The agent account whose broker works in this project's folder (owns its checkout).
    account: Mapped[str] = mapped_column(String(32))
    owner: Mapped[str | None] = mapped_column(String(64), index=True)  # username; members see only their own
    # "local": the built-in project grouping one account's local workspaces on this machine (local_workspace), found
    # by account, never by slug. None: a registered project.
    builtin: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class LaunchSpec(Base):
    """Immutable launch inputs plus the files compiled from them. Resume reuses it verbatim."""
    __tablename__ = "launch_spec"
    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(16))
    inputs: Mapped[dict] = mapped_column(JSON)
    files: Mapped[dict] = mapped_column(JSON)
    digest: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class MultiRun(Base):
    __tablename__ = "multirun"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("project.id"))
    owner: Mapped[str | None] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(40))
    winner_session_id: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class MultiRunEntry(Base):
    __tablename__ = "multirun_entry"
    group_id: Mapped[str] = mapped_column(ForeignKey("multirun.id"), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    choice: Mapped[dict] = mapped_column(JSON)
    session_id: Mapped[str | None] = mapped_column(ForeignKey("session.id"))
    error: Mapped[str | None] = mapped_column(Text)


class Session(Base):
    __tablename__ = "session"
    placement_id: Mapped[int | None] = mapped_column(ForeignKey("placement.id"))
    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # acc id, also run dir name
    project_id: Mapped[int | None] = mapped_column(ForeignKey("project.id"))
    provider: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(64))
    tmux_name: Mapped[str] = mapped_column(String(80), unique=True)
    native_id: Mapped[str | None] = mapped_column(String(64))
    launch_spec_id: Mapped[int] = mapped_column(ForeignKey("launch_spec.id"))
    desired_state: Mapped[str] = mapped_column(String(16), default="running")  # running | stopped | archived
    # acc: launched and supervised by ACC as the agent user.
    # external: started elsewhere (e.g. a root systemd unit); ACC only ingests its hooks.
    management: Mapped[str] = mapped_column(String(16), default="acc", server_default="acc")
    run_as: Mapped[str] = mapped_column(String(32))  # the agent account
    run_dir: Mapped[str | None] = mapped_column(Text)  # where its hooks write; None: run_dir/<id> (before accounts)
    owner: Mapped[str | None] = mapped_column(String(64), index=True)  # username
    external_unit: Mapped[str | None] = mapped_column(String(120))
    # Local-workspace sessions run in their own folder (local/<topic>) instead of a project's checkout.
    workdir: Mapped[str | None] = mapped_column(Text)
    # A project session in its own git worktree (<root>/.worktrees/<slug>-<id>) on its own branch: parallel agents
    # on one project don't touch each other's files.
    worktree: Mapped[str | None] = mapped_column(Text)
    automation: Mapped[dict | None] = mapped_column(JSON)

    multirun_id: Mapped[str | None] = mapped_column(String(32), index=True)
    multirun_stop_pending: Mapped[bool] = mapped_column(default=False)
    team_stop_pending: Mapped[bool] = mapped_column(default=False)
    team_host: Mapped[str | None] = mapped_column(String(40))
    team_repo: Mapped[str | None] = mapped_column(Text)
    team_id: Mapped[str | None] = mapped_column(String(32), index=True)
    parent_session_id: Mapped[str | None] = mapped_column(String(32))
    team_depth: Mapped[int] = mapped_column(default=0)
    helper_kind: Mapped[str | None] = mapped_column(String(16))
    team_task: Mapped[str | None] = mapped_column(Text)
    worktree_branch: Mapped[str | None] = mapped_column(String(120))
    # Runs in a container (its project had containers on when it was created; kept for every resume). Each launch
    # is a new generation: accd's grant and the root helper's record must name the same one.
    container: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    container_generation: Mapped[int | None] = mapped_column(Integer)
    observed_state: Mapped[str] = mapped_column(String(24), default="starting")
    status_source: Mapped[str | None] = mapped_column(String(32))
    status_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    idle_since: Mapped[datetime | None] = mapped_column(UTCDateTime())
    sleep_paused_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    activity: Mapped[str | None] = mapped_column(String(200))  # "Running npm test": from tool hooks, cleared at turn end
    activity_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    base_commit: Mapped[str | None] = mapped_column(String(40))  # the workspace's HEAD when it was first launched
    changes: Mapped[dict | None] = mapped_column(JSON)  # summary since then: {git, files, additions, deletions}
    changes_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    transcript_stamp: Mapped[str | None] = mapped_column(String(64))  # its conversation file when last indexed
    indexed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    # Ids of the vault keys it was given (never values): what redaction replaces in what it produces.
    vault_keys: Mapped[list | None] = mapped_column(JSON)
    issue_source: Mapped[dict | None] = mapped_column(JSON)
    remote_url: Mapped[str | None] = mapped_column(Text)
    recovery_attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_error: Mapped[str | None] = mapped_column(Text)
    spool_offset: Mapped[int] = mapped_column(Integer, default=0)
    last_output_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)

    placement: Mapped[Placement | None] = relationship()
    deleted_project: Mapped[dict | None] = mapped_column(JSON)
    project: Mapped[Project | None] = relationship()
    launch_spec: Mapped[LaunchSpec] = relationship()
    runs: Mapped[list["SessionRun"]] = relationship(back_populates="session", order_by="SessionRun.id")

    @property
    def host(self) -> str:
        return self.placement.host if self.placement else (self.team_host or (self.project.host if self.project else (self.deleted_project or {}).get("host")) or "local")

    @property
    def cwd(self) -> str:
        return self.placement.path if self.placement else (self.worktree or self.workdir or (self.project.workspace_path if self.project else ""))


class SessionRun(Base):
    """One process incarnation inside the session's tmux pane."""
    __tablename__ = "session_run"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("session.id"))
    reason: Mapped[str] = mapped_column(String(16))  # launch | resume | manual_resume
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    exit_status: Mapped[int | None] = mapped_column(Integer)
    exit_signal: Mapped[int | None] = mapped_column(Integer)
    end_reason: Mapped[str | None] = mapped_column(String(32))
    pid: Mapped[int | None] = mapped_column(Integer)

    session: Mapped[Session] = relationship(back_populates="runs")


class Event(Base):
    """Append-only activity stream; also the feed a future visualization would consume."""
    __tablename__ = "event"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[str | None] = mapped_column(ForeignKey("session.id"), index=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    source: Mapped[str] = mapped_column(String(24))  # claude_hook | codex_hook | tmux | supervisor | api
    kind: Mapped[str] = mapped_column(String(48))
    verified: Mapped[bool] = mapped_column(Boolean, default=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class Observation(Base):
    """Latest provider observation per key, e.g. Claude statusline rate limits."""
    __tablename__ = "observation"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)
    source: Mapped[str] = mapped_column(String(32))
    observed_at: Mapped[float] = mapped_column(Float)


class Node(Base):
    """Another machine whose brokers this ACC drives (multi-host). pending: has an enrollment code; active: enrolled
    (its Ed25519 key verifies every connection); revoked: removed."""
    __tablename__ = "node"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(40), unique=True)  # what projects' and sessions' `host` names
    status: Mapped[str] = mapped_column(String(16), default="pending")
    role: Mapped[str | None] = mapped_column(String(16))  # None until enrollment reports its OS
    offline_threshold_seconds: Mapped[int] = mapped_column(default=600, server_default="600")
    idle_sleep_minutes: Mapped[int | None] = mapped_column(Integer)  # None: off
    downtime_policy: Mapped[dict | None] = mapped_column(JSON)
    downtime_until_reconnect: Mapped[bool] = mapped_column(default=False, server_default="0")
    public_git_backup: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    last_reconnected_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    offline_since: Mapped[datetime | None] = mapped_column(UTCDateTime())
    snoozed_until: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_downtime_seconds: Mapped[int | None] = mapped_column()
    public_key: Mapped[str | None] = mapped_column(String(64))  # base64 raw Ed25519
    code_hash: Mapped[str | None] = mapped_column(String(64))
    code_expires: Mapped[datetime | None] = mapped_column(UTCDateTime())
    info: Mapped[dict] = mapped_column(JSON, default=dict)  # os, arch, hostname, version, accounts
    last_seen: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Setting(Base):
    """A choice made in Settings: "guardrails" (instance-wide), "budget.<username>" (one person's, opt-in)."""
    __tablename__ = "setting"
    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)
    updated_by: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str | None] = mapped_column(String(200))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class User(Base):
    __tablename__ = "user"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    totp_secret: Mapped[str] = mapped_column(String(64))
    totp_last_step: Mapped[int | None] = mapped_column(Integer)
    passkey_handle: Mapped[str | None] = mapped_column(String(64), unique=True)
    recovery_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    # owner: everything, including people and host settings. member: their own projects and sessions.
    role: Mapped[str] = mapped_column(String(16), default="member", server_default="member")
    account: Mapped[str | None] = mapped_column(String(32))  # the agent account their sessions run as (None: default)
    # Stable person ID (docs/ORGANIZATIONS-STABLE-IDS.md), assigned when an external identity is first bound:
    # uuid5(saved installation namespace, "user:<id>"), the same value the phase 0 dry-run proposes.
    person_id: Mapped[str | None] = mapped_column(String(36), unique=True)
    # Made by company sign-in (pre-provisioned or invited): no local password or authenticator anyone knows. Such a
    # member adds a passkey only right after an MFA-proven company sign-in (AuthSession.passkey_enroll_until).
    company_only: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")


class RetiredUser(Base):
    """A removed person's username, kept for good: ownership compares usernames, so a name is never given to anyone
    else (prerelease reviews X1/D9/Q1). Their projects, sessions and keys were given to the owner who removed them, or
    archived (`resources`)."""
    __tablename__ = "retired_user"
    username: Mapped[str] = mapped_column(String(64), primary_key=True)
    role: Mapped[str | None] = mapped_column(String(16))
    account: Mapped[str | None] = mapped_column(String(32))
    removed_by: Mapped[str | None] = mapped_column(String(64))
    resources: Mapped[str | None] = mapped_column(String(16))  # transfer | archive | None (removed before 0024)
    removed_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Invite(Base):
    """A one-time link to join ACC (a new user) or to reset an existing user's password and authenticator."""
    __tablename__ = "invite"
    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    role: Mapped[str] = mapped_column(String(16), default="member")
    account: Mapped[str | None] = mapped_column(String(32))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"))  # set: a reset
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class AuthSession(Base):
    __tablename__ = "auth_session"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    csrf: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    user_agent: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())

    reauthenticated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    passkey_verified: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    method: Mapped[str | None] = mapped_column(String(16))  # "oidc": company sign-in; None: local credentials
    # A company sign-in without proven MFA while passkeys are required: usable only for a passkey check until
    # passkey_verified (auth.lookup_session).
    step_up: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # A company-only member's MFA-proven company sign-in may add one passkey until then, without verifying again.
    passkey_enroll_until: Mapped[datetime | None] = mapped_column(UTCDateTime())
    device_fingerprint: Mapped[str | None] = mapped_column(String(64))
    device_bound_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    user: Mapped[User] = relationship()


class AttentionItem(Base):
    """Something the owner should look at. Raised and auto-resolved by verified signals only."""
    __tablename__ = "attention_item"
    snoozed_until: Mapped[datetime | None] = mapped_column(UTCDateTime())
    id: Mapped[int] = mapped_column(primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(120), index=True)
    session_id: Mapped[str | None] = mapped_column(ForeignKey("session.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))  # needs_input | recovery_needed | crash_recovered | stop_failure | auth
    severity: Mapped[str] = mapped_column(String(16), default="warn")  # info | warn | critical
    source: Mapped[str] = mapped_column(String(32))
    verified: Mapped[bool] = mapped_column(Boolean, default=True)
    title: Mapped[str] = mapped_column(String(200))
    detail: Mapped[str | None] = mapped_column(Text)
    risk: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)
    acked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    owner: Mapped[str | None] = mapped_column(String(64), index=True)  # username; None: owners only (host, ACC)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class PublishRequest(Base):
    """A request to publish (or unpublish) a host through Caddy + Cloudflare. Agents ask; the owner approves."""
    __tablename__ = "publish_request"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    action: Mapped[str] = mapped_column(String(16))  # publish | unpublish
    host: Mapped[str] = mapped_column(String(253), index=True)
    port: Mapped[int | None] = mapped_column(Integer)
    access: Mapped[str | None] = mapped_column(String(16))  # public | internal
    project_id: Mapped[int | None] = mapped_column(ForeignKey("project.id"))
    session_id: Mapped[str | None] = mapped_column(ForeignKey("session.id"))
    requested_by: Mapped[str] = mapped_column(String(80))
    owner: Mapped[str | None] = mapped_column(String(64), index=True)  # whose project or session asked
    reason: Mapped[str | None] = mapped_column(Text)
    # pending_approval | approved | applying | applied | failed | rejected | removed
    status: Mapped[str] = mapped_column(String(24), default="pending_approval")
    zone_id: Mapped[str | None] = mapped_column(String(64))
    zone_name: Mapped[str | None] = mapped_column(String(253))
    dns_record_id: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[str | None] = mapped_column(Text)
    # What the approver must see (D18), recorded with the request: {"port_owner": who reserved the port,
    # "replaces": the live site it would retarget ({id, port, access, project})}.
    context: Mapped[dict | None] = mapped_column(JSON)
    decided_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class ToolRequest(Base):
    __tablename__ = "tool_request"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    session_id: Mapped[str | None] = mapped_column(ForeignKey("session.id"))
    project_id: Mapped[int | None] = mapped_column(ForeignKey("project.id"))
    owner: Mapped[str] = mapped_column(String(64), index=True)
    account: Mapped[str] = mapped_column(String(32))
    host: Mapped[str] = mapped_column(String(64))
    manager: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(200))
    version: Mapped[str | None] = mapped_column(String(100))
    operation: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(24), default="pending_approval")
    decided_by: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[str | None] = mapped_column(Text)
    resolved_version: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class InstalledTool(Base):
    __tablename__ = "installed_tool"
    # Hash of host/account/manager/name: a single current pin, not a second copy on reinstallation.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner: Mapped[str] = mapped_column(String(64), index=True)
    host: Mapped[str] = mapped_column(String(64))
    account: Mapped[str] = mapped_column(String(32))
    manager: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(200))
    version: Mapped[str] = mapped_column(String(100))
    request_id: Mapped[str] = mapped_column(ForeignKey("tool_request.id"))
    installed_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class PortReservation(Base):
    """ACC's port registry: which project (or outside service) a local port belongs to, so projects never collide
    and a member can only publish their own projects' ports."""
    __tablename__ = "port_reservation"
    port: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("project.id", ondelete="SET NULL"))
    label: Mapped[str | None] = mapped_column(String(120))  # outside services (no project)
    owner: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class PushSubscription(Base):
    """One device that receives push notifications (Web Push subscription)."""
    __tablename__ = "push_subscription"
    id: Mapped[int] = mapped_column(primary_key=True)
    endpoint: Mapped[str] = mapped_column(Text, unique=True)
    p256dh: Mapped[str] = mapped_column(String(200))
    auth: Mapped[str] = mapped_column(String(64))
    username: Mapped[str] = mapped_column(String(64))
    label: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    last_ok_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    failures: Mapped[int] = mapped_column(Integer, default=0)


class ModelEndpoint(Base):
    """A model server agents can use instead of their vendor's models: a local LLM server (Ollama, LM Studio,
    llama.cpp, vLLM) or a compatible hosted API (DeepSeek, OpenRouter, …). Owners manage them; everyone can launch on
    them. The key never leaves accd except into a session's 0600 environment file."""
    __tablename__ = "model_endpoint"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(20))  # ollama | openai | anthropic | custom: how its models are listed
    base_url: Mapped[str] = mapped_column(String(300))  # the server, e.g. http://localhost:11434 (Ollama's model list)
    # The API styles it speaks and the URL a client uses for each: {"openai": "<url ending in /v1>",
    # "anthropic": "<url clients append /v1/messages to>"}.
    styles: Mapped[dict] = mapped_column(JSON, default=dict)
    api_key: Mapped[str | None] = mapped_column(Text)
    # The context window its models are served with (Ollama: OLLAMA_CONTEXT_LENGTH), so CLIs compact before the server
    # truncates. None: let each CLI assume its own.
    context_tokens: Mapped[int | None] = mapped_column(Integer)
    models: Mapped[list] = mapped_column(JSON, default=list)  # [{"id", "label"}], refreshed from the server
    models_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_error: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class VaultKey(Base):
    """An API key in the vault (docs/VAULT.md): stored once, named, given to projects. The value is AES-256-GCM
    ciphertext (acc.server.vault) bound to this row's id and variable; nothing ever reads it back out over the API."""
    __tablename__ = "vault_key"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(80))  # shown everywhere, e.g. "OpenAI (work)"
    variable: Mapped[str] = mapped_column(String(64))  # the environment variable sessions see, e.g. OPENAI_API_KEY
    ciphertext: Mapped[str] = mapped_column(Text)  # base64(nonce + AES-GCM ciphertext and tag)
    owner: Mapped[str] = mapped_column(String(64), index=True)  # username
    shared: Mapped[bool] = mapped_column(Boolean, default=False)  # private: only its owner's launches use it
    remote: Mapped[bool] = mapped_column(Boolean, default=False)  # sent to sessions on other machines
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)  # the value, name or settings changed
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class VaultRetired(Base):
    """A value a key no longer has (replaced, or the key deleted) that sessions were given (D22): kept, still sealed
    with its key's id and variable, so their output stays redacted. Dropped once no session lists the key."""
    __tablename__ = "vault_retired"
    id: Mapped[int] = mapped_column(primary_key=True)
    key_id: Mapped[str] = mapped_column(String(32), index=True)  # no foreign key: the key may be gone
    name: Mapped[str] = mapped_column(String(80))
    variable: Mapped[str] = mapped_column(String(64))
    ciphertext: Mapped[str] = mapped_column(Text)
    retired_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class VaultGrant(Base):
    """A key given to a project: that project's sessions get it, when the person launching may use it."""
    __tablename__ = "vault_grant"
    key_id: Mapped[str] = mapped_column(ForeignKey("vault_key.id", ondelete="CASCADE"), primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("project.id", ondelete="CASCADE"), primary_key=True)
    given_by: Mapped[str] = mapped_column(String(64))
    server_only: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    given_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Team(Base):
    """A person's explicit grant. Helpers never receive a way to edit it."""
    __tablename__ = "team"
    attempt_counter: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    protocol_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # lead session id
    limits: Mapped[dict] = mapped_column(JSON)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    stopped: Mapped[bool] = mapped_column(default=False)
    wrapup_until: Mapped[datetime | None] = mapped_column(UTCDateTime())
    resume_members: Mapped[list] = mapped_column(JSON, default=list, server_default='[]')
    lifetime_revision: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    stop_reason: Mapped[str | None] = mapped_column(String(20))
    started: Mapped[int] = mapped_column(default=0)


class TeamRequest(Base):
    __tablename__ = "team_request"
    session_id: Mapped[str] = mapped_column(ForeignKey("session.id"), primary_key=True)
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict | None] = mapped_column(JSON)
    delivered: Mapped[bool] = mapped_column(default=False)

class Schedule(Base):
    """A person's saved launch and calendar; deletion retains history."""
    __tablename__ = "schedule"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner: Mapped[str] = mapped_column(String(64), index=True)
    account: Mapped[str] = mapped_column(String(32))
    launch: Mapped[dict] = mapped_column(JSON)
    timing: Mapped[dict] = mapped_column(JSON)
    timezone: Mapped[str] = mapped_column(String(100))
    missed: Mapped[str] = mapped_column(String(8), default="once")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    next_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class ScheduleRun(Base):
    """Durable intent, including a reserved session id before any broker effect."""
    __tablename__ = "schedule_run"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    schedule_id: Mapped[str] = mapped_column(ForeignKey("schedule.id"), index=True)
    due_at: Mapped[datetime] = mapped_column(UTCDateTime())
    trigger: Mapped[str] = mapped_column(String(8))  # timer | person
    launch: Mapped[dict] = mapped_column(JSON)
    reserved_session_id: Mapped[str] = mapped_column(String(32), unique=True)
    session_id: Mapped[str | None] = mapped_column(ForeignKey("session.id"), unique=True)
    status: Mapped[str] = mapped_column(String(16), index=True)  # pending/claimed/running/completed/failed/skipped/cancelled
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Group(Base):
    __tablename__ = "acc_group"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    created_by: Mapped[str] = mapped_column(String(64))
    home_site: Mapped[str] = mapped_column(String(64), default="local")
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    board_revision: Mapped[int] = mapped_column(Integer, default=1)
    concurrent_ceiling: Mapped[int] = mapped_column(Integer, default=4)
    daily_ceiling: Mapped[int] = mapped_column(Integer, default=50)
    usage_warning_usd: Mapped[float | None] = mapped_column(Float)
    concurrent: Mapped[int] = mapped_column(Integer, default=4)
    daily_launches: Mapped[int] = mapped_column(Integer, default=50)
    override_until: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class GroupMembership(Base):
    __tablename__ = "group_membership"
    __table_args__ = (CheckConstraint("role IN ('maintainer', 'member', 'viewer')"),
                      CheckConstraint("provenance IN ('local', 'identity-provider')"))
    group_id: Mapped[str] = mapped_column(ForeignKey("acc_group.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(16))
    provenance: Mapped[str] = mapped_column(String(24), default="local")
    external_id: Mapped[str | None] = mapped_column(String(200))


class Placement(Base):
    """Routing snapshot, never permission to use another person's broker or path."""
    __tablename__ = "placement"
    __table_args__ = (UniqueConstraint("project_id", "person", "host", "account", "path"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("project.id"))
    person: Mapped[str | None] = mapped_column(String(64))
    host: Mapped[str] = mapped_column(String(64))
    account: Mapped[str] = mapped_column(String(32))
    path: Mapped[str] = mapped_column(Text)
    trust: Mapped[str] = mapped_column(String(16), default="pending")
    sync_status: Mapped[str] = mapped_column(String(24), default="unprovisioned")
    approved_by: Mapped[str | None] = mapped_column(String(64))
    machine_account_id: Mapped[str | None] = mapped_column(ForeignKey('group_machine_account.id'))


class GroupProjectOwner(Base):
    __tablename__ = "group_project_owner"
    project_id: Mapped[int] = mapped_column(ForeignKey("project.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), primary_key=True)


class GroupMergeApproval(Base):
    """An exact proposed ref transition approved by a named project maintainer; never executes Git."""
    __tablename__ = "group_merge_approval"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("project.id"))
    approved_by: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer)
    branch: Mapped[str] = mapped_column(String(200))
    old_sha: Mapped[str] = mapped_column(String(64))
    new_sha: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class GroupKeyGrant(Base):
    """Explicit consent; phase 1 records it but does not deliver keys."""
    __tablename__ = "group_key_grant"
    id: Mapped[int] = mapped_column(primary_key=True)
    key_id: Mapped[str] = mapped_column(ForeignKey("vault_key.id", ondelete="CASCADE"))
    group_id: Mapped[str] = mapped_column(ForeignKey("acc_group.id"))
    project_id: Mapped[int | None] = mapped_column(ForeignKey("project.id"))
    host: Mapped[str | None] = mapped_column(String(64))
    given_by: Mapped[str] = mapped_column(String(64))
    given_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class InfrastructureCredential(Base):
    """Separate namespace: intentionally no project grants or generic vault references."""
    __tablename__ = "infrastructure_credential"
    migration_source: Mapped[str | None] = mapped_column(String(32), unique=True)
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    provider: Mapped[str] = mapped_column(String(32))
    purpose: Mapped[str] = mapped_column(String(32))
    owner: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(default=1)
    ciphertext: Mapped[str] = mapped_column(Text)
    retired: Mapped[list] = mapped_column(JSON, default=list)  # sealed generations, retained 24h
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class DNSBinding(Base):
    __tablename__ = "dns_binding"
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    provider: Mapped[str] = mapped_column(String(32))
    zone: Mapped[str] = mapped_column(String(253))
    certificate_id: Mapped[str] = mapped_column(ForeignKey("infrastructure_credential.id"))
    publishing_id: Mapped[str] = mapped_column(ForeignKey("infrastructure_credential.id"))
    revision: Mapped[int] = mapped_column(default=1)
    applied_revision: Mapped[int | None] = mapped_column()
    status: Mapped[str] = mapped_column(String(32), default="saved")
    generation: Mapped[str | None] = mapped_column(String(64))


class Passkey(Base):
    __tablename__ = 'passkey'
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('user.id', ondelete='CASCADE'), index=True)
    credential_id: Mapped[str] = mapped_column(String(1400), unique=True)
    public_key: Mapped[str] = mapped_column(Text)
    rp_id: Mapped[str] = mapped_column(String(253))
    name: Mapped[str] = mapped_column(String(100))
    sign_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class PasskeyChallenge(Base):
    __tablename__ = 'passkey_challenge'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    challenge: Mapped[str] = mapped_column(String(128))
    purpose: Mapped[str] = mapped_column(String(16))
    origin: Mapped[str] = mapped_column(String(300))
    client: Mapped[str] = mapped_column(String(100))
    session_hash: Mapped[str | None] = mapped_column(String(64))
    user_id: Mapped[int | None] = mapped_column(ForeignKey('user.id', ondelete='CASCADE'))
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())


class PasskeyPolicy(Base):
    __tablename__ = 'passkey_policy'
    id: Mapped[int] = mapped_column(primary_key=True)
    owners: Mapped[str] = mapped_column(String(16), default='allow')
    members: Mapped[str] = mapped_column(String(16), default='allow')


class TelemetryCounter(Base):
    """Daily aggregates only, never individual events."""
    __tablename__ = 'telemetry_counter'
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    day: Mapped[str] = mapped_column(String(10), index=True)
    event: Mapped[str] = mapped_column(String(40))
    provider: Mapped[str] = mapped_column(String(20), default='')
    count: Mapped[int] = mapped_column(Integer, default=0)


class TelemetryItem(Base):
    """Immutable preview bytes, review queue and 90-day local transmission history."""
    __tablename__ = 'telemetry_item'
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    key_id: Mapped[str] = mapped_column(String(22))
    body: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(16))
    created: Mapped[float] = mapped_column(Float)
    due: Mapped[float] = mapped_column(Float)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[str] = mapped_column(String(32), default='')
    receipt: Mapped[str] = mapped_column(String(36), default='')
    occurrences: Mapped[int] = mapped_column(Integer, default=1)
    sent_at: Mapped[float | None] = mapped_column(Float)


class DNSOwnedRecord(Base):
    """Durable intent and exact remote snapshot. Pending/uncertain rows require operator reconciliation."""
    __tablename__ = 'dns_owned_record'
    __table_args__ = (UniqueConstraint('provider', 'zone_id', 'host'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))
    zone_id: Mapped[str] = mapped_column(String(600))
    host: Mapped[str] = mapped_column(String(253))
    state: Mapped[str] = mapped_column(String(24), default='pending')
    desired: Mapped[dict | None] = mapped_column(JSON)
    prior: Mapped[dict | None] = mapped_column(JSON)
    applied: Mapped[dict | None] = mapped_column(JSON)
    request_id: Mapped[str] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Device(Base):
    """Public device metadata only. Keys and import bundles never enter the database. kind browser: a person's
    paired device; kind native: an app installation with its own key; kind node: an enrolled machine's identity (node_id; person stays empty), whose superseded leaf
    is retired at retire_after at the latest."""
    __tablename__ = 'device'
    __table_args__ = (UniqueConstraint('person', 'kind', 'installation_id', name='uq_device_installation'),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    person: Mapped[str] = mapped_column(String(64), index=True)
    label: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(16), default='browser')
    status: Mapped[str] = mapped_column(String(16), default='active')
    certificate_pem: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    serial: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    installation_id: Mapped[str | None] = mapped_column(String(32))
    platform: Mapped[str | None] = mapped_column(String(16))
    key_hash: Mapped[str | None] = mapped_column(String(64))
    node_id: Mapped[str | None] = mapped_column(String(32), index=True)
    retire_after: Mapped[datetime | None] = mapped_column(UTCDateTime())


class DevicePairing(Base):
    __tablename__ = 'device_pairing'
    kind: Mapped[str] = mapped_column(String(16), default='browser', server_default='browser')
    installation_id: Mapped[str | None] = mapped_column(String(32))
    platform: Mapped[str | None] = mapped_column(String(16))
    csr_pem: Mapped[str | None] = mapped_column(Text)
    renew_fingerprint: Mapped[str | None] = mapped_column(String(64))
    key_hash: Mapped[str | None] = mapped_column(String(64))
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    person: Mapped[str] = mapped_column(String(64), index=True)
    label: Mapped[str] = mapped_column(String(100))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    receipt_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(16), default='pending')
    device_id: Mapped[str | None] = mapped_column(String(32))
    renew_id: Mapped[str | None] = mapped_column(String(32))
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class GroupCheckout(Base):
    """Broker-created checkout receipt; its ID is reserved for exactly one live session."""
    __tablename__ = 'group_checkout'
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    placement_id: Mapped[int] = mapped_column(ForeignKey('placement.id'))
    base_sha: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class GroupWork(Base):
    __tablename__ = 'group_work'
    session_id: Mapped[str] = mapped_column(ForeignKey('session.id'), primary_key=True)
    checkout_id: Mapped[str] = mapped_column(ForeignKey('group_checkout.id'), unique=True)
    task: Mapped[str] = mapped_column(String(500))
    base_sha: Mapped[str] = mapped_column(String(40))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    heartbeat: Mapped[datetime | None] = mapped_column(UTCDateTime())


class GroupClaim(Base):
    __tablename__ = 'group_claim'
    __table_args__ = (UniqueConstraint('session_id', 'path'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey('session.id'))
    path: Mapped[str] = mapped_column(String(300))
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())


class GroupOperation(Base):
    __tablename__ = 'group_operation'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    group_id: Mapped[str] = mapped_column(ForeignKey('acc_group.id'), index=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey('project.id'))
    kind: Mapped[str] = mapped_column(String(32))
    summary: Mapped[str] = mapped_column(String(500))
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class GroupLaunch(Base):
    """One reservation for each explicit launch or resume, retained after stops and failures."""
    __tablename__ = 'group_launch'
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey('session.id'), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class OIDCProvider(Base):
    """The one company identity provider (docs/ORGANIZATIONS.md, phase 1). Off until an owner turns it on. The client
    secret is AES-GCM sealed with the vault's data key under its own purpose-bound associated data, never returned."""
    __tablename__ = 'oidc_provider'
    id: Mapped[int] = mapped_column(primary_key=True)  # always 1: one provider per installation
    issuer: Mapped[str] = mapped_column(String(300))
    client_id: Mapped[str] = mapped_column(String(200))
    secret_ciphertext: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    revision: Mapped[int] = mapped_column(Integer, default=1)  # bumps on every change: in-flight sign-ins end
    session_hours: Mapped[int] = mapped_column(Integer, default=12)  # how long a company sign-in lasts (1-24)
    # Tenant-specific acr values that prove MFA, besides amr (oidc.mfa_proven); empty: amr only.
    mfa_acr: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    # Company-only members may add a passkey right after an MFA-proven company sign-in; owners can turn that off.
    passkey_enrollment: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    updated_by: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class ExternalIdentity(Base):
    """A verified (issuer, subject) pair bound to one person. Never keyed by email or display name."""
    __tablename__ = 'external_identity'
    __table_args__ = (UniqueConstraint('organization', 'issuer', 'subject', name='uq_external_identity'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    organization: Mapped[str] = mapped_column(String(64), default='installation')
    issuer: Mapped[str] = mapped_column(String(300))
    subject: Mapped[str] = mapped_column(String(255))
    person_id: Mapped[str] = mapped_column(ForeignKey('user.person_id', ondelete='CASCADE'), index=True)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class SSOInvite(Base):
    """An owner's one-time permission for an unknown subject's first company sign-in: a new member, or linking an
    existing member (who must also be signed in locally, recently)."""
    __tablename__ = 'sso_invite'
    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    username: Mapped[str | None] = mapped_column(String(64))  # a new member's name
    # The invited person's directory account (email or UPN, lower case): the verified claims must name it.
    email: Mapped[str] = mapped_column(String(320))
    account: Mapped[str | None] = mapped_column(String(32))
    user_id: Mapped[int | None] = mapped_column(ForeignKey('user.id', ondelete='CASCADE'))  # set: link an existing member
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    withdrawn_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class OIDCFlow(Base):
    """One started company sign-in: one use, ten minutes, bound to the browser that started it."""
    __tablename__ = 'oidc_flow'
    state_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    binding_hash: Mapped[str] = mapped_column(String(64))
    nonce: Mapped[str] = mapped_column(String(64))
    verifier: Mapped[str] = mapped_column(String(128))
    provider_revision: Mapped[int] = mapped_column(Integer)
    invite_id: Mapped[int | None] = mapped_column(ForeignKey('sso_invite.id', ondelete='CASCADE'))
    session_hash: Mapped[str | None] = mapped_column(String(64))  # linking: the local sign-in that started it
    client: Mapped[str] = mapped_column(String(64), default='', index=True)  # the address (IPv6 /64) that started it
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())


class GroupMachineAccount(Base):
    """Owner-reviewed person identity on an immutable enrolled node/account."""
    __tablename__ = 'group_machine_account'
    # Approval reserves a node/account for good (also once revoked); pending requests don't, so nobody can block the
    # rightful person by asking first.
    __table_args__ = (Index('uq_group_machine_account_reserved', 'node_id', 'account', unique=True,
                            sqlite_where=text('approved_by IS NOT NULL'),
                            postgresql_where=text('approved_by IS NOT NULL')),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    node_id: Mapped[str] = mapped_column(ForeignKey('node.id'))
    person: Mapped[str] = mapped_column(String(64))
    account: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default='pending')
    proof_verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    approved_by: Mapped[str | None] = mapped_column(String(64))


# Orchestrator v2 slices keep their tables in their own modules (src/acc/server/orchestrator), on this Base: importing
# the package here registers them with every metadata user (create_all, migrations' checks) without editing this file.
from . import orchestrator as _orchestrator  # noqa: E402,F401


class SecurityPolicy(Base):
    __tablename__ = 'security_policy'
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    scope: Mapped[dict] = mapped_column(JSON)
    overrides: Mapped[dict] = mapped_column(JSON, default=dict)
    registry_version: Mapped[int] = mapped_column(Integer, default=1)
    previous: Mapped[dict | None] = mapped_column(JSON)


class SecurityRevision(Base):
    __tablename__ = 'security_revision'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)


class SecurityChange(Base):
    __tablename__ = 'security_change'
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    actor: Mapped[str] = mapped_column(String(64))
    session_hash: Mapped[str] = mapped_column(String(64))
    digest: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    applied: Mapped[bool] = mapped_column(Boolean, default=False)


class SecurityFolderPolicy(Base):
    """Desired/applied folder state; only the folder lane may validate/apply OS grants."""
    __tablename__ = 'security_folder_policy'
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    machine_id: Mapped[str] = mapped_column(String(32))
    account: Mapped[str] = mapped_column(String(32))
    roots: Mapped[list] = mapped_column(JSON, default=list)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    applied_revision: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[str] = mapped_column(String(20), default='pending')
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_by: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)
