"""Request bodies and response shapes for the API."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re

from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import object_session

from ... import providers
from ...providers.base import LEGACY_OPTION_FIELDS
from .. import automation
from ..automation import capability
from ..compiler import LaunchInputs
from ..models import Node, Project, PublishRequest, Session, utcnow


class ProjectIn(BaseModel):
    slug: str = Field(max_length=64)
    name: str = Field(min_length=1, max_length=200)
    workspace_path: str = Field(max_length=500)
    repo: str | None = Field(None, max_length=200, pattern=r"^$|^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")  # owner/name
    remote_url: str | None = Field(None, max_length=500)
    import_mode: str = Field("managed", pattern="^(managed|readonly)$")
    notes: str | None = Field(None, max_length=5000)
    host: str = Field("local", pattern=r"^[a-z0-9][a-z0-9-]{0,39}$")  # "local", or an enrolled node (multi-host)
    # On another machine: which of its agent accounts runs the sessions (default: yours, if it has it)
    account: str | None = Field(None, pattern=r"^[a-z_][a-z0-9_-]{0,31}$")

    @field_validator("name")
    @classmethod
    def _visible_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("enter a name")
        return value


class IssueRef(BaseModel):
    source: str = Field(pattern=r"^(gitlab|gitea|linear)$")
    kind: str = Field("issue", pattern=r"^(issue|merge_request|pull_request)$")
    id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")


class SessionIn(BaseModel):
    project_id: int | None = None  # None with a topic: a local workspace (Researcher, Council)
    topic: str | None = Field(None, max_length=80)
    workspace: str | None = Field(None, pattern=r"^[a-z0-9][a-z0-9-]{0,79}$")  # an existing local workspace folder
    provider: str = Field(pattern=providers.ID_PATTERN)
    name: str = Field(min_length=1, max_length=40)
    # Every value here ends up on the CLI's command line: each is checked against what it may be.
    model: str | None = Field(None, pattern=r"^$|^[A-Za-z0-9][A-Za-z0-9._:\[\]/-]{0,79}$")
    effort: str | None = Field(None, pattern=r"^$|^[a-z]{1,16}$")
    permission_mode: str = Field("acceptEdits", pattern=r"^(default|acceptEdits|plan|auto|dontAsk|bypassPermissions)$")
    sandbox: str = Field("workspace-write", pattern=r"^(read-only|workspace-write|danger-full-access)$")
    approval: str = Field("on-request", pattern=r"^(untrusted|on-request|never)$")
    remote_control: bool = False
    options: dict[str, str | bool] = Field(default_factory=dict, max_length=20)  # other providers' launch options
    endpoint: str | None = Field(None, pattern=r"^$|^[a-z0-9][a-z0-9-]{0,38}$")  # a model endpoint instead of the vendor's
    automation: dict = Field(default_factory=dict)
    orchestrator: dict | None = None
    worktree: bool = False  # a project session in its own git worktree and branch
    budget_ok: bool = False  # launch even though the person's own budget pauses this agent
    initial_prompt: str | None = Field(None, max_length=20000)
    issue_source: IssueRef | None = None
    confirm_dangerous: bool = False
    profile: str | None = Field(None, pattern=r"^$|^[a-z0-9][a-z0-9-]{0,63}$")
    skills: list[str] | None = Field(None, max_length=200)  # None = the profile's defaults
    agents: list[str] | None = Field(None, max_length=200)
    mcps: list[str] | None = Field(None, max_length=200)

    @field_validator("name")
    @classmethod
    def _visible_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("enter a name")
        return value

    @field_validator("provider")
    @classmethod
    def _known_provider(cls, value: str) -> str:
        if value not in providers.PROVIDERS:
            raise ValueError(f"unknown agent; choose one of: {', '.join(providers.ids())}")
        return value

    @model_validator(mode="before")
    @classmethod
    def _legacy_options(cls, data):
        """permission_mode, sandbox, approval and remote_control are fields of their own: one sent inside `options`
        (where the other launch options go) is moved there, rather than silently ignored at launch."""
        if isinstance(data, dict) and isinstance(data.get("options"), dict):
            options = dict(data["options"])
            for key in LEGACY_OPTION_FIELDS:
                if key in options:
                    value = options.pop(key)
                    data = {**data, key: data.get(key, value), "options": options}
        return data

    @field_validator("options")
    @classmethod
    def _option_values(cls, value: dict) -> dict:
        for key, item in value.items():  # the provider's schema checks values; these only bound their shape
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", key) or (isinstance(item, str) and not re.fullmatch(r"[A-Za-z0-9._:/-]{0,80}", item)):
                raise ValueError(f"the option {key!r} has a value Rynhult doesn't accept")
        return value

    @field_validator("initial_prompt")
    @classmethod
    def _prompt_is_not_an_option(cls, value: str | None) -> str | None:
        # The prompt is the CLI's last argument: one starting with "-" would be read as a flag
        # (e.g. --dangerously-skip-permissions), bypassing the dangerous-launch confirmation.
        if value and value.startswith("-"):
            raise ValueError("the first prompt can't start with '-' (the agent would read it as a setting). Start it with a word instead")
        return value

    def inputs(self, catalog=None) -> LaunchInputs:
        fields = self.model_dump(exclude={"project_id", "topic", "workspace", "name", "confirm_dangerous", "skills", "agents", "mcps",
                                          "worktree", "budget_ok", "orchestrator", "issue_source", "automation"})
        fields = {k: (v or None) if isinstance(v, str) and k in ("model", "effort", "initial_prompt", "profile", "endpoint") else v
                  for k, v in fields.items()}
        profile = catalog.profiles.get(self.profile or "") if catalog else None
        for kind in ("skills", "agents", "mcps"):
            chosen = getattr(self, kind)
            if chosen is None and profile is not None:  # profile defaults, limited to what this provider can use
                pool = getattr(catalog, kind)
                chosen = [x for x in profile.get(kind, []) if self.provider in pool[x].providers]
            fields[kind] = tuple(chosen or ())
        if profile is not None:  # the profile's defaults for this provider, unless the request set them
            defaults = profile.get(self.provider, {})
            options = dict(fields.get("options") or {})
            for key, value in defaults.items():
                if key in LEGACY_OPTION_FIELDS:
                    if key not in self.model_fields_set:
                        fields[key] = value
                elif key not in options:
                    options[key] = value
            fields["options"] = options
        try:
            automation.validate(self.automation, self.provider)
            if self.endpoint and (self.automation.get("autoresume") or self.automation.get("goal")):
                raise ValueError("Goal and auto-resume work only on the agent's own plan, not a model endpoint. Turn them off or choose the plan.")
        except ValueError as error:
            raise providers.CompileError(str(error)) from error
        goal = self.automation.get('goal')
        if goal:
            fields['append_prompt'] = (
                f"Goal: {goal['text']}\nReport a standalone line GOAL: done when achieved "
                "or GOAL: blocked: <why> when blocked. "
                f"Rynhult allows at most {goal['max_continuations']} continuations over {goal['max_hours']} hours.")
            fields['initial_prompt'] = fields.get('initial_prompt') or f"Work toward this goal: {goal['text']}"
        return LaunchInputs(**fields)


class PromoteIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    github: bool = True  # create a private repo (protect-main) and push the workspace; False keeps it local
    description: str = Field("", max_length=300)
    confirm: bool = False  # creating a GitHub repository is outward-facing


class DeleteWorkspaceIn(BaseModel):
    confirm: str = Field(max_length=80)  # the folder name, typed by the owner: deleting removes its files for good


class RenameIn(BaseModel):
    name: str = Field(min_length=1, max_length=40, pattern=r"^[\w][\w .:-]*$")


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)  # typed into the session's prompt (the broker checks it again)

    @field_validator("text")
    @classmethod
    def _plain_text(cls, value: str) -> str:
        if re.search(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", value):  # keys and escape sequences, not words
            raise ValueError("the message can't contain control characters (newlines and tabs are fine)")
        if not value.strip():
            raise ValueError("type a message first")
        return value


class ExternalSessionIn(BaseModel):
    project_id: int
    provider: str = Field(pattern="^(claude|codex)$")
    name: str = Field(min_length=1, max_length=40)
    native_id: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    run_as: str = Field(pattern=r"^[a-z_][a-z0-9_-]{0,31}$")
    unit: str = Field(min_length=1, max_length=120)


class PublishIn(BaseModel):
    target_ip: str | None = Field(default=None, max_length=128)
    host: str = Field(min_length=3, max_length=253)
    port: int
    access: str = Field(pattern="^(public|internal)$")
    project_id: int | None = None
    reason: str | None = None
    replace: bool = False  # the host is live: retarget it (D18)


class ApproveIn(BaseModel):
    confirm_replace: bool = False  # the request retargets a live site, and the approver saw that (D18)


class LoginCodeIn(BaseModel):
    code: str = Field(min_length=1, max_length=512)


class LoginIn(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=1024)
    code: str = Field(max_length=16)


class SetupBeginIn(BaseModel):
    setup_token: str


class InviteIn(BaseModel):
    role: str = Field("member", pattern=r"^(owner|member)$")
    account: str | None = Field(None, pattern=r"^[a-z_][a-z0-9_-]{0,31}$")  # the agent account their sessions run as
    create_account: bool = False  # create that agent account first (it must end in -agent)


class InviteBeginIn(BaseModel):
    token: str = Field(max_length=128)


class InviteCompleteIn(BaseModel):
    token: str = Field(max_length=128)
    username: str | None = Field(None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$")  # new users only
    password: str = Field(min_length=12, max_length=256)
    code: str = Field(max_length=16)


class PasswordIn(BaseModel):
    current_password: str = Field(max_length=1024)
    new_password: str = Field(min_length=12, max_length=256)


class SetupCompleteIn(BaseModel):
    setup_token: str = Field(max_length=128)
    username: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$")  # shown in audit lines and file comments
    password: str = Field(min_length=12, max_length=256)
    code: str = Field(max_length=16)


def machine_offline(row, host: str, offline_hosts: set[str] | None = None) -> bool:
    if host == 'local':
        return False
    if offline_hosts is not None:  # a list resolves every machine once (offline_machines), not once per row
        return host in offline_hosts
    db = object_session(row)
    if not db:
        return False
    node = db.scalar(select(Node).where(Node.name == host))
    return bool(node and node.status == 'active' and node.offline_since is not None)


def offline_machines(db) -> set[str]:
    """The names machine_offline() is true for: active machines that are offline now."""
    return set(db.scalars(select(Node.name).where(Node.status == 'active', Node.offline_since.is_not(None))))


def etag_matches(if_none_match: str, etag: str) -> bool:
    """If-None-Match uses the weak comparison (RFC 9110, 13.1.2): `W/"x"` matches `"x"`, and `*` matches any current
    list. A proxy that compresses a response may hand the browser a weak tag; an exact comparison would then never
    revalidate and every poll would send the whole list again."""
    tags = {tag.strip() for tag in if_none_match.split(",")}
    return "*" in tags or etag in {tag[2:] if tag.startswith("W/") else tag for tag in tags}


def json_list(value, if_none_match: str | None = None) -> Response:
    """Large lists (thousands of rows) as JSON without FastAPI's generic per-field encoder, which is about ten
    times slower. The same bytes: datetimes are aware, and jsonable_encoder writes them with isoformat() too.

    Polled lists also carry an ETag. The browser revalidates every poll (private, no-cache) and gets an empty 304
    while nothing changed, instead of the whole list again: 2 MB every 3 s for 2,000 sessions on a phone."""
    body = json.dumps(value, default=lambda o: o.isoformat(), ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode()
    etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
    headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
    if if_none_match and etag_matches(if_none_match, etag):
        return Response(status_code=304, headers=headers)
    return Response(body, media_type="application/json", headers=headers)


def project_out(project: Project) -> dict:
    return {k: getattr(project, k) for k in
            ("id", "slug", "name", "host", "managed_checkout", "workspace_path", "repo", "remote_url", "private_workspace", "import_mode", "trusted", "archived", "notes",
             "compose_autostart", "git_backup", "containers", "account", "owner")} | {
        "machine_offline": machine_offline(project, project.host or "local"),
        "compose_approved": bool((project.compose_pin or {}).get("digest"))}


class CreateRepoIn(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    description: str = Field("", max_length=2000, pattern=r"^[^\x00]*$")
    confirm: bool = False


class NewProjectIn(BaseModel):
    """A new project made from the dashboard, always in the caller's own workspace root: an empty git folder, or a
    clone of a repository they can reach (owner/name through their gh login, or an HTTPS or SSH URL)."""
    name: str = Field(min_length=1, max_length=200)
    description: str = Field("", max_length=2000, pattern=r"^[^\x00]*$")
    slug: str | None = Field(None, max_length=64)
    source: str = Field("empty", pattern="^(empty|clone)$")
    url: str | None = Field(None, max_length=500)
    trust: bool | None = None  # unset: an empty folder is trusted, a clone is not (it brings its own config and hooks)
    host: str = Field("local", pattern=r"^[a-z0-9][a-z0-9-]{0,39}$")  # "local", or a connected machine
    account: str | None = Field(None, pattern=r"^[a-z_][a-z0-9_-]{0,31}$")  # there: which of its agent accounts

    @field_validator("name")
    @classmethod
    def _visible_name(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("give the project a name")
        return value


class ImportRepoIn(BaseModel):
    repo: str = Field(pattern=r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
    slug: str | None = None
    trust: bool = False


class RuntimeIn(BaseModel):
    compose_autostart: bool
    digest: str | None = Field(None, pattern=r"^[0-9a-f]{64}$")  # the reviewed compose files (needed to turn it on)


class BackupIn(BaseModel):
    enabled: bool


def session_out(session: Session, detail: bool = False, ssh_host: str = "", offline_hosts: set[str] | None = None) -> dict:
    evidence_at = max((t for t in (session.status_at, session.activity_at) if t is not None), default=session.created_at)
    stale_hook = (session.observed_state == "active" and (session.status_source or "").endswith("_hook")
                  and (utcnow() - evidence_at).total_seconds() >= 600)
    out = {
        "machine_offline": machine_offline(session, session.host, offline_hosts),
        "automation": session.automation or {},
        "automation_capability": capability(session.provider),
        "id": session.id, "project_id": session.project_id, "project": session.project.slug if session.project else (session.deleted_project or {}).get("slug", "Deleted project"),
        "provider": session.provider, "name": session.name, "tmux_name": session.tmux_name,
        "native_id": session.native_id, "desired_state": session.desired_state,
        "observed_state": session.observed_state, "status_source": session.status_source,
        "status_at": session.status_at, "last_output_at": session.last_output_at,
        "status_stale_since": evidence_at if stale_hook else None,
        "sleep_paused_at": session.sleep_paused_at,
        "activity": session.activity, "activity_at": session.activity_at, "changes": session.changes,
        "changes_at": session.changes_at,
        "remote_url": session.remote_url, "recovery_attempts": session.recovery_attempts,
        "next_attempt_at": session.next_attempt_at, "last_error": session.last_error,
        "created_at": session.created_at, "model": session.launch_spec.inputs.get("model"),
        "permission_mode": session.launch_spec.inputs.get("permission_mode"),
        "management": session.management, "run_as": session.run_as, "external_unit": session.external_unit,
        "owner": session.owner,
        "multirun_id": session.multirun_id,
        "team_stop_pending": session.team_stop_pending, "team_id": session.team_id, "parent_session_id": session.parent_session_id, "team_depth": session.team_depth,
        "workdir": session.workdir, "standalone": bool(session.workdir), "cwd": session.cwd,
        "worktree": session.worktree, "worktree_branch": session.worktree_branch, "host": session.host,
        "container": session.container,
        "repo": None if not session.project or session.project.private_workspace else session.project.repo,
    }
    if detail:
        out["launch_spec"] = {"inputs": session.launch_spec.inputs, "files": session.launch_spec.files,
                              "digest": session.launch_spec.digest}
        out["runs"] = [{k: getattr(r, k) for k in ("id", "reason", "started_at", "verified_at", "ended_at",
                                                   "exit_status", "exit_signal", "end_reason", "pid")}
                       for r in session.runs]
        local = (session.host) == "local" and session.management != "external"
        # "Open in editor": VS Code Remote-SSH to this machine, in the session's folder (its worktree if it has one)
        out["editor"] = {"ssh_host": ssh_host, "path": session.cwd} if local and ssh_host else None
    return out


def _publish_member_text(value, target_ip=None):
    """Legacy details can embed an address even when there is no pinned context metadata."""
    if isinstance(value, dict):
        return {key: _publish_member_text(item, target_ip) for key, item in value.items()}
    if isinstance(value, list):
        return [_publish_member_text(item, target_ip) for item in value]
    if not isinstance(value, str):
        return value
    if target_ip:
        value = value.replace(target_ip, '[address redacted]')
    def replace(match):
        candidate = match[0].rstrip('.')
        try:
            ipaddress.ip_address(candidate)
        except ValueError:
            return match[0]
        return '[address redacted]' + match[0][len(candidate):]
    value = re.sub(r'(?<![\w])[0-9A-Fa-f:.]+(?:%[A-Za-z0-9_.-]+)?(?![\w])', replace, value)
    # IPv4 with a :port suffix is not itself an IP literal; redact the address independently.
    return re.sub(r'(?<![\w])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![\w])',
                  lambda match: '[address redacted]' if all(int(part) <= 255 for part in match[0].split('.'))
                  else match[0], value)


def publish_out(r: PublishRequest, *, is_owner: bool = False) -> dict:
    context = r.context or {}
    result = {k: getattr(r, k) for k in ("id", "action", "host", "port", "access", "project_id", "session_id",
                                       "requested_by", "reason", "status", "zone_name", "detail", "decided_by",
                                       "created_at", "decided_at", "completed_at")} | {
        "replaces": context.get("replaces"), "port_owner": context.get("port_owner")}
    outcome = context.get('outcome')
    if is_owner:
        return result | {'target_ip': context.get('target_ip'), 'outcome': outcome}
    if outcome:
        # Only these typed facts are member-visible; helper/HTTPS text and DNS addresses are owner metadata.
        result['outcome'] = {key: outcome[key] for key in ('operation', 'route_applied', 'route_removed', 'dns_removed')
                             if key in outcome}
        if 'dns' in outcome:
            result['outcome']['dns'] = {key: outcome['dns'][key] for key in ('host', 'dns_only') if key in outcome['dns']}
        if 'https' in outcome:
            result['outcome']['https'] = {'checked': outcome['https'].get('checked', False)}
    return _publish_member_text(result, context.get('target_ip'))


class DevelopmentWorkspaceIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    kind: str = Field("development", pattern="^development$")
