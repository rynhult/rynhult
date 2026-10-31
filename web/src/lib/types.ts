export type Provider = string;  // a provider id from /api/providers/registry ("claude", "codex", …)

export interface ProviderOption {
  key: string; label: string; kind: "select" | "bool"; default: string | boolean | null; help: string;
  choices: { value: string; label: string }[]; dangerous: (string | boolean)[];
}
export interface ProviderInfo {
  id: string; label: string; short: string; color: string; tier: "full" | "standard" | "basic"; preview: boolean;
  install: string; homepage: string; usage_url: string; login: "paste" | "device" | "link" | null;
  supports: string[]; options: ProviderOption[]; endpoint_styles: string[]; installed?: boolean | null;
  login_hint?: string;  // no sign-in Rynhult drives: how to give it models instead
  model_format?: string;  // how its model ids are written ("provider/model")
  preview_note?: string;  // what has been verified, for a preview
}
/* Launch options that have always been top-level fields of a launch (Claude's and Codex's); others go in `options`. */
export const LEGACY_OPTIONS = ["permission_mode", "sandbox", "approval", "remote_control"];
export type ObservedState =
  | "starting" | "active" | "waiting_input" | "idle" | "exited"
  | "recovering" | "recovery_needed" | "stopped" | "archived";

export interface Project {
  machine_offline?: boolean;
  id: number;
  slug: string;
  name: string;
  managed_checkout?: boolean;
  account?: string;  // the agent account its sessions run as
  host: string;
  workspace_path: string;
  repo: string | null;
  private_workspace?: boolean;

  remote_url?: string | null;
  import_mode: "managed" | "readonly";
  trusted: boolean;
  archived: boolean;
  notes: string | null;
  compose_autostart: boolean;
  /** An owner approved its compose files (autostart runs the stack only while they still match). */
  compose_approved: boolean;
  git_backup: boolean;
  /** New sessions run in containers (an owner's switch; only while containers are on for this Rynhult). */
  containers?: boolean;
}

export interface IssueRef { source: "gitlab" | "gitea" | "linear"; kind: "issue" | "merge_request" | "pull_request"; id: string }
export interface SourceIssue extends IssueRef {
  identifier: string; title: string; description: string; url: string; state: string;
  labels: string[]; assignee: string; updated_at: string | null;
}
export interface IssueConnections {
  remote_url?: string | null;
  gitlab?: { base_url: string; key_id: string; server_id?: string };
  gitea?: { base_url: string; key_id: string; server_id?: string };
  linear?: { key_id: string; team_id: string; project_id?: string; label_id?: string; comment_pr: boolean };
  comments?: { session_id: string; session: string; status: string; error?: string | null }[];
}

/** Whether a stack's compose files are what an owner approved (GET /api/projects/{id}/runtime, "compose"). */
export interface ComposeReview {
  approved: boolean;
  digest: string;
  diff?: string;
  truncated?: boolean;
  note?: string | null;
  at?: string | null;
  by?: string | null;
}

/** A project's git backup on the main Rynhult (GET /api/projects/{id}/backup). */
export interface BackupView {
  enabled: boolean;
  configured: boolean;
  git?: boolean;
  checked_at?: number;
  last_at?: string;
  failed_at?: string;
  error?: string;
  note?: string | null;
  kept?: { ref: string; as: string; reason: string; at: string }[];
  repo: null | { branches: { name: string; sha: string; date: string | null }[]; tags: number; size: number };
}

export interface Container { Service: string; Name: string; State: string; Status: string; Health: string }

export interface Run {
  id: number;
  reason: string;
  started_at: string;
  verified_at: string | null;
  ended_at: string | null;
  exit_status: number | null;
  exit_signal: number | null;
  end_reason: string | null;
  pid: number | null;
}

export interface SessionSummary {
  sleep_paused_at?: string | null;
  machine_offline?: boolean;
  automation?: import("../components/SessionAutomation").Automation;

  multirun_id?: string | null;
  changes_at?: string | null;
  git?: { branch?: string; ahead?: number; lfs?: boolean; commits?: { sha: string; subject: string }[]; error?: string };
  team_stop_pending?: boolean;
  team_id?: string | null; parent_session_id?: string | null; team_depth?: number;
  id: string;
  project_id: number | null;
  project: string;
  provider: Provider;
  name: string;
  tmux_name: string;
  native_id: string | null;
  desired_state: "running" | "stopped" | "archived";
  observed_state: ObservedState;
  status_source: string | null;
  status_at: string | null;
  status_stale_since?: string | null;
  last_output_at: string | null;
  activity: string | null;  // what it is doing now (tool hooks)
  activity_at: string | null;
  worktree: string | null;
  host?: string;  // the machine it runs on (multi-host)
  worktree_branch: string | null;
  container?: boolean;  // runs in a container (acc-containers), in its own private checkout
  repo?: string | null;  // the project's GitHub repository (none for workspaces)
  changes: { git: boolean; files?: number; additions?: number; deletions?: number; branch?: string | null;
    comparison_ref?: string; since_launch?: boolean } | null;
  remote_url: string | null;
  recovery_attempts: number;
  next_attempt_at: string | null;
  last_error: string | null;
  created_at: string;
  model: string | null;
  permission_mode: string | null;
  management: "acc" | "external";
  run_as: string;
  external_unit: string | null;
  workdir: string | null;
  standalone: boolean;
  cwd: string;
}

export interface MultiRunChoice {
  provider: Provider; profile: string | null; model: string; options: Record<string, string | boolean>;
  confirm_dangerous: boolean;
}

export interface MultiRunGroup {
  id: string; name: string; project_id: number | null; project: string; owner: string; created_at: string;
  winner_session_id: string | null;
  runs: {
    position: number; choice: MultiRunChoice; error: string | null; session: SessionSummary | null;
    usage: { cost: { total_cost_usd?: number } | null; context_window: { used_percentage?: number; total_input_tokens?: number; total_output_tokens?: number } | null;
      source: string; observed_at: number } | null;
    ci: { run: NonNullable<GithubSnapshot["runs"]>[number] | null; error: string | null; source: string; observed_at: number } | null;
  }[];
  archive_errors?: { session_id: string; error: string }[];
}

export interface SessionDetail extends SessionSummary {
  launch_spec: { inputs: Record<string, unknown>; files: Record<string, string>; digest: string };
  runs: Run[];
  editor: { ssh_host: string; path: string } | null;  // "Open in editor" (local sessions, when Rynhult knows its SSH host)
}

export interface AccEvent {
  id: number;
  ts: string;
  source: string;
  kind: string;
  verified: boolean;
  payload: Record<string, unknown>;
}

export interface Observation {
  key: string;
  value: Record<string, any>;
  source: string;
  observed_at: number;
}

export interface AuthState {
  setup_required: boolean;
  authenticated: boolean;
  username: string | null;
  role?: "owner" | "member";
  managed_checkout?: boolean;
  account?: string;  // the agent account this user's sessions run as
  sso?: boolean;  // the sign-in page offers company sign-in (organizations mode, turned on by an owner)
  sign_in_method?: "local" | "oidc";
  step_up_required?: boolean;  // a company sign-in without proven MFA: a passkey check comes first
  instance?: {
    host: string; agent_user: string; wildcard_zone: string; version?: string; build?: string; source_url?: string;
    source_download?: string | null;  // this exact version's source (tar.gz), once install.sh has built it
    private_ca?: boolean;  // Rynhult is served with a private CA's certificate (Settings → Certificate)
    workspaces_root?: string;  // where New project makes your projects' folders
    github?: boolean;  // Rynhult has a GitHub token, so owners can create repositories
    features?: Record<string, boolean>;
    organizations?: boolean;  // organizations mode (ACC_ORGANIZATIONS=1): Settings shows Company sign-in to owners
    orchestrator?: boolean;  // effective Orchestrator v2 availability (on by default): its team views, gates and selection settings
  };
}

export interface AttentionItem {
  image_check?: { project_id: number; provider: string; model: string } | null;
  bulk_dismissible?: boolean;
  id: number;
  kind: "needs_input" | "recovery_needed" | "crash_recovered" | "stop_failure" | "auth" | "publish_request" | "publish_failed" | "compose" | string;
  severity: "info" | "warn" | "critical";
  source: string;
  verified: boolean;
  title: string;
  detail: string | null;
  risk?: { label: "Routine" | "Review" | "Risky"; reason: string } | null;
  session_id: string | null;
  session_name: string | null;
  project: string | null;
  created_at: string;
  updated_at: string;
  acked_at: string | null;
  resolved_at: string | null;
  snoozed_until?: string | null;
  /** Operational alerts stay open after acknowledgement until resolved or snoozed. */
  persistent?: "machine_offline" | "backup_failed" | "backup_stale" | null;
  publish_id: string | null;
  /** A publish request's target, and the live site it would retarget (approving that needs confirming). */
  publish?: {
    host: string; port: number | null; access: "public" | "internal" | null; replaces: PublishReplaces | null;
    /** As on Publishing (UX review IN-1): null port_owner means nobody reserved the port. */
    action?: "publish" | "unpublish"; requested_by?: string; reason?: string | null; project?: string | null;
    port_owner?: PublishRequest["port_owner"];
  } | null;
  tool?: ToolRequest | null;
}

export interface ToolPin {
  id: string; host: string; account: string; manager: string; name: string; version: string | null;
  owner: string; destination: string;
}
export interface ToolRequest extends ToolPin {
  operation: "install" | "remove"; reason: string; status: string; detail: string | null;
  resolved_version: string | null; created_at: string;
}

export interface PublishReplaces { id: string; port: number | null; access: "public" | "internal" | null; project: string | null; decided_by: string | null }

export interface ProviderHealth { logged_in: boolean; email?: string; plan?: string; subscription?: string; login_expires_at?: number; error?: string; [key: string]: unknown }
export interface AccountHealth {
  [provider: string]: ProviderHealth | string | number | string[] | undefined;
  claude?: { logged_in: boolean; email?: string; subscription?: string; rate_limit_tier?: string; login_expires_at?: number; error?: string };
  codex?: { logged_in: boolean; email?: string; plan?: string; last_refresh?: string; status?: string; error?: string };
  github?: { logged_in: boolean; login?: string | null; scopes?: string | null; error?: string };
  checked_at?: number;
  account?: string;  // the agent account these logins belong to
  accounts?: string[];
  default_account?: string;
}

export interface LoginFlow {
  flow_id?: string;
  provider: string;
  url: string | null;
  user_code: string | null;
  done: boolean;
  ok: boolean;
  message: string;
}

export interface PublishRequest {
  target_ip?: string | null;
  id: string;
  action: "publish" | "unpublish";
  host: string;
  port: number | null;
  access: "public" | "internal" | null;
  project_id: number | null;
  session_id: string | null;
  requested_by: string;
  reason: string | null;
  status: "pending_approval" | "approved" | "applying" | "applied" | "failed" | "rejected" | "removed";
  zone_name: string | null;
  detail: string | null;
  decided_by: string | null;
  created_at: string;
  decided_at: string | null;
  completed_at: string | null;
  /** D18: the live site this request would retarget, and who reserved its port (null: nobody). */
  replaces?: PublishReplaces | null;
  port_owner?: { project: string | null; label: string | null } | null;
}

export interface CatalogItem {
  id: string;
  kind: "skill" | "agent" | "mcp";
  name?: string;
  source: string;
  license: string;
  providers: Provider[];
  notes?: string;
  file_count: number;
  url?: string;
  command?: string;
  args?: string[];
  env_keys?: string[];
  origin?: "repo" | "local" | "restored";
  added_by?: string;
  added_at?: string;
}

export interface CatalogProfile {
  id: string;
  name: string;
  description: string;
  skills: string[];
  agents: string[];
  mcps: string[];
  providers?: Provider[];
  claude?: { permission_mode?: string };
  codex?: { sandbox?: string; approval?: string };
  [provider: string]: unknown;  // each provider's launch defaults, by its option keys
  instructions: string;
  standalone?: boolean;
  origin?: "repo" | "override" | "local";
}

export interface ImportFlag { level: "info" | "warn" | "bad"; message: string; files: string[] }

export interface ImportPreview {
  kind: "skill" | "agent" | "choose";
  repo: string;
  commit: string;
  license: string;
  path?: string;
  source?: string;
  token?: string;
  suggested_id?: string;
  name?: string;
  description?: string;
  files?: { path: string; bytes: number; text: string }[];
  skipped?: { path: string; reason: string }[];
  flags?: ImportFlag[];
  candidates: { path: string; url: string }[];
}

export interface Catalog {
  revision: string;
  skills: CatalogItem[];
  agents: CatalogItem[];
  mcps: CatalogItem[];
  profiles: CatalogProfile[];
  deferred: { id: string; reason: string }[];
}

export interface HostLive { cpu_percent: number; cpu_count: number; load: number[]; memory: { total: number; used: number; percent: number }; swap_percent: number }

export interface HostView {
  live: HostLive;
  snapshot: null | {
    collected_at: number; hostname: string; kernel: string; uptime_seconds: number; reboot_required: boolean;
    disks: { mount: string; device: string; total: number; used: number; free: number; percent: number }[];
    smart: { device: string; model: string; size: string; passed: boolean | null; temperature: number | null; power_on_hours: number | null; percentage_used: number | null; media_errors: number | null }[];
    gpu: { name: string; utilization: number | null; memory_used_mb: number | null; memory_total_mb: number | null; temperature: number | null }[];
    services?: { units: Record<string, string>; failed: string[] };  // owners only
    containers: { name: string; image: string; state: string; status: string; project: string | null }[];
    tailscale: { state: string; ips: string[]; online: boolean };
    accounts?: HostAccount[];
  };
  git_backup?: { repos: number; bytes: number; enabled: boolean };
}

/** The periodic summary a machine sends (acc.hostinfo.light): the status board's numbers. */
export interface HostLight {
  collected_at: number; cpu_percent: number | null; memory_percent: number | null; cpu_count: number | null;
  load: (number | null)[] | null; uptime_seconds: number | null; disk: { mount: string; percent: number; free: number | null } | null;
}

/** Another machine's details (GET /api/host?machine=…, owners): what its acc-node can read without root. Parts it
 *  can't read are null and named in `unavailable`. */
export interface RemoteHostView {
  last_backup_via_public_address?: string | null;
  planned_downtime?: { active: boolean };
  machine: string; connected: boolean; supported: boolean; version: string | null; os: string | null; last_seen: string | null;
  received_at: number | null; light: HostLight | null;
  live: (Omit<HostLive, "cpu_percent" | "cpu_count" | "load"> & { cpu_percent: number | null; cpu_count: number | null; load: (number | null)[] | null }) | null;
  snapshot: null | {
    collected_at: number; hostname: string; kernel: string; system: "linux" | "wsl" | "mac" | "windows" | null;
    uptime_seconds: number | null; reboot_required: boolean;
    disks: NonNullable<HostView["snapshot"]>["disks"] | null;
    gpu: NonNullable<HostView["snapshot"]>["gpu"] | null;
    services: { units: Record<string, string>; failed: string[] } | null;
    tailscale: NonNullable<HostView["snapshot"]>["tailscale"] | null;
    smart: null; containers: null; accounts: null;
    unavailable: string[];
  };
}

/** One broker unit: an agent account (or the services one), its use now and its fair-use limits. */
export interface HostAccount {
  account: string; role: "agents" | "services"; active: string;
  memory_bytes: number | null; memory_max: number | null; cpu_percent: number | null; cpu_quota_percent: number | null;
  tasks: number | null; tasks_max: number | null; workspace_root: string | null; disk_bytes: number | null;
}

/** A project's GitHub view (GET /api/projects/{id}/github), refreshed by the supervisor. */
export interface GithubSnapshot {
  error?: string;
  fetched_at?: number;
  repo?: { full_name: string; html_url: string; private: boolean; default_branch: string; pushed_at: string; description?: string };
  branches?: { name: string; protected: boolean; sha: string }[];
  commits?: { sha: string; message: string; author: string; date: string; url: string }[];
  pulls?: { number: number; title: string; author: string; draft: boolean; head: string; updated_at: string; url: string }[];
  issues?: { number: number; title: string; author: string; labels: string[]; updated_at: string; url: string }[];
  runs?: { id: number; name: string; branch: string; event: string; status: string; conclusion: string | null; created_at: string; url: string }[];
}

export interface PullCheck { name: string; kind: "check" | "status"; status: string; conclusion: string | null; url: string | null; required?: boolean }

/** A session's pull request, from GitHub through the person's own sign-in (Pull request tab). */
export interface PullStatus {
  provider: "github";
  repo: string;
  repo_url: string;
  branch: string;
  default_branch: string;
  pushed: boolean;
  signed_in: "token" | "gh";
  pull: {
    number: number; title: string; body: string; url: string; state: "open" | "closed" | "merged"; draft: boolean;
    base: string; head_sha: string; mergeable: boolean | null; mergeable_state: string | null; reviewers: string[]; author: string | null;
  } | null;
  checks: PullCheck[];
  threads: number;
  merge: { methods: ("merge" | "squash" | "rebase")[]; blockers: string[]; notes: string[]; required: string[]; allowed: boolean } | null;
}

export interface SessionChanges {
  revision: string;
  git: boolean;
  base?: string | null;
  since_launch?: boolean;
  comparison_ref?: string;
  branch?: string | null;
  files?: number;
  additions?: number;
  deletions?: number;
  list: { path: string; status: string; additions: number; deletions: number; binary: boolean }[];
  diff: string;
  truncated?: boolean;
}

export interface Artifact {
  name: string;
  kind: "image" | "video" | "text";
  size: number;
  mtime: number;
  too_large: boolean;
}

export interface SessionArtifacts {
  files: Artifact[];
  truncated: boolean;
}

export interface SearchResponse {
  query: string;
  indexed_sessions?: number;
  results: {
    session: { id: string; name: string; project: string; provider: Provider; observed_state: ObservedState; desired_state: string; created_at: string; workdir: string | null };
    matches: number;
    hits: { role: "user" | "assistant"; ts: string | null; seq: number; snippet: string }[];
  }[];
}

/** GET /api/board (owners): every machine's summary and alerts, and every project's machine and backup state. */
export interface BoardMachine {
  planned_downtime?: { active: boolean };
  name: string; label: string; local: boolean; connected: boolean; last_seen?: string | null; version: string | null;
  os: string | null; supported: boolean; update_available: boolean; light: HostLight | null; light_at: number | null;
  stale?: boolean; alerts: { severity: "info" | "warn" | "critical"; title: string }[];
}
export interface BoardView {
  generated_at: number;
  machines: BoardMachine[];
  projects: { id: number; slug: string; name: string; host: string; git_backup: boolean;
    backup: { last_at?: string | null; failed_at?: string | null; error?: string | null; checked_at?: number | null; note?: string | null } | null }[];
}

export type HAHealth = {
  enabled: boolean;
  role: "active" | "standby";
  epoch?: number;
  database: "ok" | "unavailable";
  automatic_failover: false;
  automatic_evaluation?: {
    evaluation_enabled: boolean;
    would_promote: boolean;
    reason: string;
    promotion_wired: false;
  };
  uncertain?: boolean;
};
