from __future__ import annotations

import os
import pwd
import secrets
import socket
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from acc.env import Environment, environment


@dataclass(frozen=True)
class Settings:
    ha_node_id: str = ""  # ACC_HA_NODE_ID: opt-in manual active/passive; every restart is standby
    ha_auto_evaluate: bool = False  # owner deployment setting; dry-run only, never promotes
    database_url: str = ""  # ACC_DATABASE_URL; empty preserves local SQLite
    data_dir: Path = Path("/srv/acc/data")
    run_dir: Path = Path("/srv/acc/run")
    # Root's own sessions (external, e.g. the admin session) keep their spool under a root-owned parent, so no
    # agent can swap the directory root writes to and reads from.
    root_run_dir: Path = Path("/srv/acc/run-root")
    broker_socket: Path = Path("/srv/acc/sock/broker.sock")
    # One broker per agent account (acc-broker@<account>.socket); unset: every account uses broker_socket.
    broker_socket_dir: Path | None = None
    broker_env_dir: Path = Path("/etc/acc/broker")  # per-account broker settings (allowed roots)
    accounts_dir: Path = Path("/srv/acc/accounts")  # requests to the acc-accounts root helper (new agent accounts)
    accounts_timeout: float = 60.0
    update_repository: str | None = None  # root-owned ACC_UPDATE_REPOSITORY, else derived from ACC_SOURCE_URL
    updates_dir: Path = Path("/srv/acc/updates")
    update_source: Path = Path(__file__).resolve().parents[3]
    packages_dir: Path = Path("/srv/acc/packages")  # requests to the acc-packages root helper (agent software)
    # Per-session containers (docs/CONTAINER-LAUNCHER.md): OFF unless ACC_CONTAINERS=1 or an owner turns the Preview on
    # in Admin features (passkey re-verification), and then only on a qualified host for projects an owner switches. accd writes each container session's grant to <containers_dir>/grants for the
    # acc-containers root helper, and reads its audit lines from <containers_dir>/audit.
    # None distinguishes an unset default from an explicit server Off; post-init normalizes these to bool.
    groups_remote_enabled: bool | None = None  # ACC_GROUPS_REMOTE: own-machine groups, owner opt-in
    containers_enabled: bool | None = None
    # Container sessions also stay refused until their terminal attach path is qualified on a real machine (the
    # broker's pane can't reach the launcher unit's private runtime: docs/decisions/drafts/container-attach-path.md).
    # ACC_CONTAINERS_ATTACH_QUALIFIED=1 only after that qualification run.
    containers_attach_qualified: bool = False
    # Containers are a Preview: a project can switch them on only once this host's root-owned qualification record
    # names a passing report for this machine (acc.container_host_record; docs/CONTAINERS.md, "Release status").
    containers_record: Path = Path("/etc/acc/containers/host-qualified.json")
    mobile_enabled: bool | None = None  # ACC_MOBILE: native lifecycle APIs; off by default
    # Organizations (docs/ORGANIZATIONS.md): OFF unless ACC_ORGANIZATIONS=1. Only then do owners see company sign-in
    # settings, and single sign-on stays off until an owner configures and turns it on there.
    organizations_enabled: bool | None = None
    containers_dir: Path = Path("/srv/acc/containers")
    containers_policy: str = "default"  # the pinned image (policy id in /etc/acc/containers/policy.json)
    services_account: str = ""  # owns the project Docker stacks (its broker only runs compose); "" = the agent account
    hook_path: str = "/srv/acc/bin/acc-hook"
    statusline_path: str = "/srv/acc/bin/acc-statusline"
    ingest_interval: float = 1.0
    reconcile_interval: float = 5.0
    container_audit_interval: float = 10.0  # how often the container launcher's audit lines are copied in
    changes_interval: float = 20.0
    history_interval: float = 30.0  # how often sessions with new activity are re-indexed for history search  # how often a working session's Changes summary is refreshed (x6 while idle)
    # recovery policy (plan §3): bounded retries, then recovery_needed
    retry_backoff: tuple[float, ...] = (0.0, 30.0, 120.0)  # delay before attempt 1, 2, 3
    claude_verify_timeout: float = 90.0
    codex_settle_seconds: float = 30.0
    quiet_idle_override: float | None = None  # tests: replaces every provider's quiet_idle_seconds
    auth_interval: float = 900.0
    models_interval: float = 86400.0  # model catalog refresh: daily
    # browser access (M3): Caddy terminates TLS for these origins
    # Where accd listens: host:port, unix:/path, or "systemd" (socket activation). In production accd.socket, which
    # only Caddy can reach,
    # so no local process can talk to accd directly or claim another client address.
    listen: str = "127.0.0.1:8740"
    allowed_origins: tuple[str, ...] = ("http://127.0.0.1:8740",)  # set ACC_ALLOWED_ORIGINS to the URL users open
    preview_domain: str = ""  # ACC_PREVIEW_DOMAIN: private wildcard suffix for isolated previews
    cookie_secure: bool = True
    ssh_host: str = ""  # ACC_SSH_HOST: how editors reach this machine over SSH ("Open in editor"); "" = the dashboard's host
    static_dir: Path = Path(__file__).parent / "static"
    # publishing (M6): public sites point at the static WAN IP, internal ones at the LAN IP
    publish_dir: Path = Path("/srv/acc/publish")
    origin_public_ip: str = ""  # One-time Publishing inventory bootstrap; later changes live in Settings
    origin_internal_ip: str = ""
    publish_timeout: float = 90.0
    # catalog (M7): vendored skills/agents/MCPs/profiles in this repository; agent user's home for Codex skill paths
    catalog_dir: Path = Path(__file__).resolve().parents[3] / "catalog"
    guide_dir: Path = Path(__file__).resolve().parents[3]  # ACC's own checkout: its docs/wiki is the in-app Guide
    # A private root CA nodes should trust (e.g. Caddy's internal one); unset: this ACC has a publicly trusted cert
    node_ca_file: Path | None = None
    # Where people using this ACC can get its source code: a fork that changes ACC sets its own
    source_url: str = "https://github.com/rynhult/rynhult"  # the public tree is rewritten to rynhult/rynhult by scripts/public-snapshot.sh
    agent_home: Path = Path("/home/agent")  # ACC_AGENT_HOME: home of the user sessions run as
    # How the dashboard names this machine and the zone whose wildcard certificate covers published names.
    host_label: str = socket.gethostname().split(".")[0]
    wildcard_zone: str = ""
    publish_zones: tuple[str, ...] = ()  # ACC_PUBLISH_ZONES: zones names may be published in (empty: any)
    catalog_local_dir: Path = Path("/srv/acc/catalog-local")  # items and profiles added from the dashboard
    # GitHub (M8): read views polled with ETags; new repos get the repo's protect-main ruleset
    github_interval: float = 180.0
    workspaces_root: Path = Path("/srv/acc/workspaces")
    # host + providers (M9)
    hoststate_path: Path = Path("/srv/acc/hoststate/hoststate.json")
    host_interval: float = 60.0
    provider_interval: float = 600.0
    ruleset_path: Path = Path(__file__).resolve().parents[3] / "deploy/github/ruleset-protect-main.json"
    # git backup (M15): one bare repository per project, pushed to by brokers over /api/git/ (docs/MULTIHOST.md)
    git_dir: Path = Path("/srv/acc/git")
    backup_url: str = ""  # ACC_BACKUP_URL: the address brokers push to; "" = the first https origin (none: off)
    backup_interval: float = 900.0  # projects with new commits are pushed at most this often (and after sessions stop)
    backup_max_push: int = 2 << 30  # bytes one push may send
    backup_timeout: float = 1800.0  # seconds one push may take

    features: object = field(default=None, repr=False, compare=False)
    feature_overrides: dict[str, bool] = field(default_factory=dict)
    _feature_config: dict[str, bool] | None = field(default=None, repr=False, compare=False)
    _feature_previous: dict[str, bool] | None = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        explicit = dict(self._feature_config or {})
        previous = self._feature_previous
        actual = {}
        for key, name in [('organizations', 'organizations_enabled'), ('containers', 'containers_enabled'),
                          ('mobile', 'mobile_enabled'), ('groups_remote', 'groups_remote_enabled')]:
            value = getattr(self, name)
            if value is not None and (previous is None or bool(value) != previous.get(key)):
                explicit[key] = value
            actual[key] = bool(value)
            object.__setattr__(self, name, bool(value))
        object.__setattr__(self, '_feature_config', explicit)
        object.__setattr__(self, '_feature_previous', actual)

    @property
    def reserved_hosts(self) -> set[str]:
        """Names never published: ACC's own hosts, and each publish zone's apex and www."""
        zones = {*self.publish_zones, *([self.wildcard_zone] if self.wildcard_zone else [])}
        own = {urlparse(origin).hostname or "" for origin in self.allowed_origins}
        return ({name for zone in zones for name in (zone, f"www.{zone}")} | own) - {""}

    def session_run_dir(self, session) -> Path:
        """Where a session's hooks write: root's sessions under the root-owned parent, the rest where they were
        created (run_dir/<account>/<id> with per-account brokers; run_dir/<id> for older sessions)."""
        if session.run_as == "root":
            return self.root_run_dir / session.id
        return Path(session.run_dir) if session.run_dir else self.run_dir / session.id

    def account_run_dir(self, account: str) -> Path:
        """The folder an account's broker creates session folders in (its ACC_RUN_DIR)."""
        return self.run_dir / account if self.broker_socket_dir is not None else self.run_dir

    def account_root(self, account: str) -> Path:
        """Where an agent account's projects and local workspaces live: the first of its broker's allowed roots
        (/etc/acc/broker/<account>.env, written by acc-agent-add); the shared workspaces root otherwise."""
        try:
            values = Environment(dict(line.split("=", 1) for line in
                                      (self.broker_env_dir / f"{account}.env").read_text().splitlines()
                                      if "=" in line and not line.startswith("#")))
            first = values.get("ACC_ALLOWED_ROOTS", "").split(":")[0].strip()
            if first.startswith("/"):
                return Path(first)
        except OSError:
            pass
        return self.workspaces_root

    @staticmethod
    def account_home(account: str) -> Path:
        try:
            return Path(pwd.getpwnam(account).pw_dir)
        except KeyError:
            return Path("/home") / account

    @property
    def default_account(self) -> str:
        """The agent account new sessions and projects use (single-user: the only one)."""
        return self.agent_user

    @property
    def services_owner(self) -> str:
        return self.services_account or self.default_account

    @property
    def agent_user(self) -> str:
        return self.agent_home.name

    @property
    def db_url(self) -> str:
        return self.database_url or f"sqlite:///{self.data_dir / 'acc.db'}"

    @property
    def setup_token_path(self) -> Path:
        return self.data_dir / "secrets" / "setup_token"

    @property
    def github_token_path(self) -> Path:
        return self.data_dir / "secrets" / "github_token"

    @property
    def cloudflare_token_path(self) -> Path:
        return self.data_dir / "secrets" / "cloudflare_token"

    @property
    def vault_key_path(self) -> Path:
        """The vault's data key (docs/VAULT.md): 32 random bytes, base64. Never in the database."""
        return self.data_dir / "secrets" / "vault.key"

    @property
    def vapid_key_path(self) -> Path:
        return self.data_dir / "secrets" / "vapid_private.pem"

    @property
    def push_subject(self) -> str:
        """Contact for push services (Apple requires one): the dashboard's https URL when there is one."""
        https = [o for o in self.allowed_origins if o.startswith("https://")]
        return https[0] if https else "mailto:acc@example.com"

    @property
    def editor_host(self) -> str:
        """The SSH host editors open session folders on: ACC_SSH_HOST, else the host of the first https origin."""
        if self.ssh_host:
            return self.ssh_host
        https = [o for o in self.allowed_origins if o.startswith("https://")]
        return (urlparse(https[0]).hostname or "") if https else ""

    @property
    def backup_base(self) -> str:
        """Where brokers push backups (this ACC's own address): ACC_BACKUP_URL, else the first https origin. ""
        turns backups off (a plain-http address would carry push tokens in the clear)."""
        if self.backup_url:
            return self.backup_url.rstrip("/")
        https = [o for o in self.allowed_origins if o.startswith("https://")]
        return https[0].rstrip("/") if https else ""

    @property
    def oidc_redirect_uri(self) -> str:
        """The one exact address the identity provider sends people back to: the first https origin (else the first)."""
        https = [o for o in self.allowed_origins if o.startswith("https://")]
        return (https or list(self.allowed_origins))[0].rstrip("/") + "/api/auth/oidc/callback"

    @property
    def bundle_dir(self) -> Path:
        """This ACC's source at its version (deploy/install.sh builds it; GET /api/nodes/bundle serves it)."""
        return self.data_dir / "bundle"

    @property
    def api_token_path(self) -> Path:
        return self.data_dir / "secrets" / "api_token"

    @classmethod
    def from_env(cls) -> "Settings":
        env = environment
        kwargs = {}
        for name, key, cast in [
            ("database_url", "ACC_DATABASE_URL", str),
            ("ha_node_id", "ACC_HA_NODE_ID", str),
            ("ha_auto_evaluate", "ACC_HA_AUTO_EVALUATE", lambda value: value == "1"),
            ("data_dir", "ACC_DATA_DIR", Path), ("run_dir", "ACC_RUN_DIR", Path), ("root_run_dir", "ACC_ROOT_RUN_DIR", Path),
            ("broker_socket", "ACC_BROKER_SOCKET", Path), ("broker_socket_dir", "ACC_BROKER_SOCKET_DIR", Path),
            ("services_account", "ACC_SERVICES_ACCOUNT", str), ("hook_path", "ACC_HOOK_PATH", str),
            ("statusline_path", "ACC_STATUSLINE_PATH", str),
            ("catalog_local_dir", "ACC_CATALOG_LOCAL_DIR", Path),
            ("host_label", "ACC_HOST_LABEL", str), ("ssh_host", "ACC_SSH_HOST", str), ("listen", "ACC_LISTEN", str), ("wildcard_zone", "ACC_WILDCARD_ZONE", str),
            ("agent_home", "ACC_AGENT_HOME", Path),
            ("origin_public_ip", "ACC_ORIGIN_PUBLIC_IP", str), ("origin_internal_ip", "ACC_ORIGIN_INTERNAL_IP", str),
            ("ingest_interval", "ACC_INGEST_INTERVAL", float),
            ("reconcile_interval", "ACC_RECONCILE_INTERVAL", float),
            ("claude_verify_timeout", "ACC_CLAUDE_VERIFY_TIMEOUT", float),
            ("codex_settle_seconds", "ACC_CODEX_SETTLE_SECONDS", float),
            ("node_ca_file", "ACC_NODE_CA_FILE", Path),
            ("source_url", "ACC_SOURCE_URL", str),
            ("preview_domain", "ACC_PREVIEW_DOMAIN", str),
            ("update_repository", "ACC_UPDATE_REPOSITORY", str),
            ("updates_dir", "ACC_UPDATE_ROOT", Path), ("update_source", "ACC_UPDATE_SOURCE", Path),
            ("git_dir", "ACC_GIT_DIR", Path), ("backup_url", "ACC_BACKUP_URL", str),
            ("backup_interval", "ACC_BACKUP_INTERVAL", float), ("backup_max_push", "ACC_BACKUP_MAX_PUSH", int),
            ("containers_dir", "ACC_CONTAINERS_DIR", Path), ("containers_policy", "ACC_CONTAINERS_POLICY_ID", str),
        ]:
            if key in env:
                kwargs[name] = cast(env[key])
        if "ACC_ALLOWED_ORIGINS" in env:
            kwargs["allowed_origins"] = tuple(o for o in env["ACC_ALLOWED_ORIGINS"].split(",") if o)
        if "ACC_PUBLISH_ZONES" in env:
            kwargs["publish_zones"] = tuple(z.strip().lower().rstrip(".") for z in env["ACC_PUBLISH_ZONES"].split(",") if z.strip())
        if "ACC_GROUPS_REMOTE" in env:
            kwargs["groups_remote_enabled"] = env["ACC_GROUPS_REMOTE"] in ("1", "true", "yes")
        if "ACC_CONTAINERS" in env:
            kwargs["containers_enabled"] = env["ACC_CONTAINERS"] in ("1", "true", "yes")
        if "ACC_CONTAINERS_ATTACH_QUALIFIED" in env:
            kwargs["containers_attach_qualified"] = env["ACC_CONTAINERS_ATTACH_QUALIFIED"] in ("1", "true", "yes")
        if "ACC_MOBILE" in env:
            kwargs["mobile_enabled"] = env["ACC_MOBILE"] in ("1", "true", "yes")
        if "ACC_ORGANIZATIONS" in env:
            kwargs["organizations_enabled"] = env["ACC_ORGANIZATIONS"] in ("1", "true", "yes")
        if "ACC_COOKIE_SECURE" in env:
            kwargs["cookie_secure"] = env["ACC_COOKIE_SECURE"] not in ("0", "false", "no")
        if "ACC_RETRY_BACKOFF" in env:
            kwargs["retry_backoff"] = tuple(float(x) for x in env["ACC_RETRY_BACKOFF"].split(","))
        return cls(**kwargs)

    def api_token(self) -> str:
        """Bootstrap API token (M2, before app login exists in M3). Created on first start."""
        path = self.api_token_path
        if not path.exists():
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_text(secrets.token_urlsafe(32))
            os.chmod(path, 0o600)
        return path.read_text().strip()
