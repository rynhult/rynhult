/* Words, not engine identifiers (UX review IN-4, CO-1). Each raw value stays reachable in a tooltip.
   Pure functions, no React, so they can be unit tested. */

export const diskMountWords = (mount: string) => mount === "/System/Volumes/Data" ? "Macintosh HD (data)" : mount;

/** Who raised an Inbox item, from its `source`. `provider` names a provider id ("claude" → "Claude Code"). */
const ITEM_SOURCES: Record<string, string> = {
  agent: "Agent request",
  agent_request: "Agent request",
  guardrails: "Guardrail",
  tools: "Tool request",
  publisher: "Publishing",
  budgets: "Budgets",
  passkeys: "Passkeys",
  schedules: "Schedules",
  autostart: "Autostart",
  backup: "Backups",
  "github actions": "GitHub Actions",
  "acc-hoststate": "Host monitor",
  psutil: "Host monitor",
  systemd: "Host monitor",
  smartctl: "Host monitor",
  "nvidia-smi": "Host monitor",
  "credentials expiry": "Sign-in check",
  "gh auth status": "Sign-in check",
  supervisor: "Rynhult",
  api: "Rynhult",
  tmux: "Process state",
  screen: "Prompt on screen",
};

export function sourceWords(raw: string, provider: (id: string) => string = (id) => id): string {
  if (ITEM_SOURCES[raw]) return ITEM_SOURCES[raw];
  if (raw.endsWith("_hook")) return provider(raw.slice(0, -5));
  if (raw.endsWith(" login status")) return "Sign-in check";
  if (raw.startsWith("session ")) return "Agent request";  // a publish request an agent made with acc-request
  return raw;  // a person's name (a request they made)
}

/** A session's Activity event in words (UX review SE-10): "Turn finished", "Using a tool: Bash", "Now Needs you".
    `state` turns an observed state ("waiting_input") into its label; the raw kind goes in a tooltip. */
const EVENT_WORDS: Record<string, string> = {
  sessionstart: "Session started", sessionend: "Session ended", userpromptsubmit: "Message sent to the agent",
  pretooluse: "Using a tool", posttooluse: "Tool finished", posttoolusefailure: "Tool failed", notification: "Notification",
  stop: "Turn finished", agentturncomplete: "Turn finished", sessionidle: "Turn finished", subagentstop: "Helper finished",
  permissionrequest: "Asked for permission", permissionasked: "Asked for permission", permissionreplied: "Permission answered",
  precompact: "Compacting the conversation",
  run_launch: "Started", run_resume: "Resumed", run_explanation: "Started to explain its changes", run_adopted: "Picked up after a Rynhult restart",
  process_exited: "The agent's process ended", process_missing: "The agent's process is gone",
  launch_failed: "Couldn't start", guardrail_blocked: "A guardrail blocked a command", crash_recovered: "Recovered after a crash",
  recovery_needed: "Recovery needed", recovery_failed: "Recovery failed", spool_parse_error: "Couldn't read an event from the agent", remote_spool_full: "Events from the other machine were dropped",
  worktree_removed: "Worktree removed", worktree_kept: "Worktree kept (it has unsaved work)", linear_comment: "Commented on the Linear issue",
  tool_request: "Asked for a tool", publish_request: "Asked to publish a site", merge_request: "Asked to merge",
};
export function eventWords(event: { kind: string; payload: Record<string, unknown> }, state: (raw: string) => string = (s) => s): string {
  if (event.kind === "state") return `Now ${state(String(event.payload.to))} (was ${state(String(event.payload.from))})`;
  const key = event.kind in EVENT_WORDS ? event.kind : event.kind.toLowerCase().replace(/[^a-z]/g, "");
  const words = EVENT_WORDS[key];
  if (!words) return event.kind.replace(/[_.]/g, " ").replace(/^./, (c) => c.toUpperCase());
  const tool = event.payload.tool_name ?? event.payload.toolName ?? event.payload.tool;
  return typeof tool === "string" && /tool/i.test(key) ? `${words}: ${tool}` : words;
}

/** A hook's notification type in words ("permission_prompt" → "asks for permission"); others pass through. */
const NOTIFICATIONS: Record<string, string> = { permission_prompt: "asks for permission", idle_prompt: "waiting for your next message",
  auth_success: "signed in", elicitation_dialog: "asks you a question" };
export const notificationWords = (raw: string) => NOTIFICATIONS[raw] ?? raw;

/** An Audit row as a sentence (UX review AU-1): "alex opened api-refactor's terminal to type". The raw action stays in a
    tooltip. Unknown actions read "alex · project autostart · demo-api". */
const AUDIT_VERBS: Record<string, (target: string, detail: Record<string, unknown>) => string> = {
  "device.pair": (_t, d) => `paired a browser for ${d.person ?? "a person"} (policy not applied)`,
  "device.rename": () => "renamed a paired browser",
  "device.renew": (_t, d) => `renewed a browser for ${d.person ?? "a person"} (policy not applied)`,
  "device.revoke": (_t, d) => `revoked a browser identity for ${d.person ?? "a person"} (policy not applied)`,
  "device.pairing_mismatch": (_t, d) => `cancelled pairing for ${d.person ?? "a person"}: the matching code differed`,
  "device.remove": (_t, d) => `removed a paired browser record for ${d.person ?? "a person"}`,
  "remote_access.prepare": () => "saved LAN pairing settings",
  "remote_access.apply": () => "applied and verified public access",
  "remote_access.off": () => "removed public access routing",
  "remote_access.failed": () => "could not verify public access; routing removal requested",
  "terminal.attach": (t, d) => `opened ${t}'s terminal ${d.mode === "write" ? (d.take ? "and took control" : "to type") : "to watch"}`,
  "terminal.read": (t) => `read ${t}'s terminal output as text`,
  "session.create": (t) => `started ${t}`, "session.stop": (t) => `stopped ${t}`, "session.resume": (t) => `resumed ${t}`,
  "session.interrupt": (t) => `interrupted ${t}`, "session.archive": (t) => `archived ${t}`, "session.rename": (t) => `renamed ${t}`,
  "preview.send_element": (t) => `sent a preview element to ${t}`,
  "session.message": (t) => `sent a message to ${t}`, "session.explain": (t) => `asked ${t} to explain its changes`,
  "session.dangerous_confirmed": (t) => `confirmed an unrestricted run for ${t}`, "session.user_exit": (t) => `ended ${t} from its terminal`,
  "project.create": (t) => `added project ${t}`, "project.import": (t) => `imported ${t}`, "project.trust": (t) => `trusted ${t}`,
  "issue_server.add": (_t, d) => `allowed git server ${d.base_url}`,
  "issue_server.confirm": (_t, d) => `re-confirmed addresses for ${d.base_url}`,
  "issue_server.remove": () => "removed an allowed git server",
  "project.archive": (t) => `archived project ${t}`, "project.autostart": (t) => `changed autostart for ${t}`,
  "publish.address_bootstrap": () => "imported Publishing bootstrap addresses from the environment",
  "publish.address_add": (_t, d) => `added publishing address ${d.label} (${d.address})`,
  "publish.address_remove": (_t, d) => `removed publishing address ${d.label} (${d.address})`,
  "publish.address_default": (_t, d) => `made ${d.label} the default publishing address`,
  "publish.approve": (t) => `approved publishing ${t}`, "publish.reject": (t) => `rejected publishing ${t}`,
  "catalog.add_mcp": (t) => `added MCP server ${t}`, "catalog.add_skill": (t) => `added skill ${t}`, "catalog.add_agent": (t) => `added subagent ${t}`,
  "catalog.create_profile": (t) => `created profile ${t}`, "catalog.edit_profile": (t) => `edited profile ${t}`,
  "auth.passkey_registered": () => "registered a passkey", "auth.passkey_removed": () => "removed a passkey",
  "auth.passkey_renamed": () => "renamed a passkey", "auth.passkey_login": () => "signed in with a passkey",
  "auth.passkey_failed": () => "failed passkey verification", "auth.passkey_verified": () => "verified with a passkey",
  "auth.passkey_recovery_created": () => "replaced their passkey recovery code", "auth.passkey_recovered": () => "recovered passkey sign-in",
  "auth.passkey_policy_changed": () => "changed passkey requirements", "auth.passkey_reauth": () => "verified with password and authenticator",
  "auth.login": () => "signed in", "auth.login_failed": () => "failed to sign in", "auth.password_changed": () => "changed their password",
  "auth.sessions_revoked": () => "signed out other devices", "user.invite": () => "invited someone", "user.invite_refused": () => "was refused an owner invite (verify first)",
  "user.reset_refused": (t) => `was refused a reset link for ${t} (verify first)`, "user.role_refused": (t) => `was refused a role change for ${t} (verify first)`,
  "user.remove": (t) => `removed ${t}`,
  "user.role": (t) => `changed ${t}'s role`, "guardrails.update": () => "changed the guardrails", "budgets.update": () => "changed budgets",
  "telemetry.consent": () => "changed usage and bug report choices", "telemetry.send": () => "attempted a telemetry send",
  "telemetry.collector": () => "changed the telemetry collector address",
  "telemetry.report": () => "updated a bug report", "telemetry.delete": () => "requested telemetry deletion",
  "vault.add": (t) => `added key ${t}`, "vault.delete": (t) => `deleted key ${t}`, "vault.give": (t) => `gave key ${t} to a project`,
  "port.reserve": (t) => `reserved port ${t}`, "port.release": (t) => `released port ${t}`,
  "pr.create": (t) => `opened a pull request for ${t}`, "pr.merge": (t) => `merged ${t}`,
  "node.policy": (t) => `changed machine role, sleep setting, offline threshold or maintenance snooze for ${t}`,
  "node.wsl_isolation": (t, d) => `${d.dismissed ? "dismissed" : "restored"} the Windows access warning for ${t}`,
  "session.sleep_pause": (t, d) => `paused ${t} so ${d.machine} can sleep after ${d.idle_minutes} idle minutes`,
  "node.offline": (t) => `machine ${t} disconnected`,
  "node.reconnect": (t, d) => `machine ${t} reconnected after ${d.downtime_seconds ?? '?'} seconds offline`,
  "node.offline_alert": (t) => `raised an offline alert for ${t}`,
  "node.check": () => 'checked all machines', "node.check_result": () => 'recorded machine check results',
  "node.add": (t) => `added machine ${t}`, "node.enroll": () => "joined as a machine", "node.remove": (t) => `removed machine ${t}`,
  "tool.approve": (t) => `approved tool ${t}`, "tool.reject": (t) => `rejected tool ${t}`,
  "project.containers": (t, d) => `${d.enabled ? "switched" : "switched off"} containers for ${t}`,
  "containers.limits": (t) => `changed container limits for ${t}`,
  "containers.limits_default": (t) => `gave ${t} its default container limits`,
  "container.launch": (t) => `started the container of ${t}`, "container.stop": (t) => `stopped the container of ${t}`,
  "container.launch_failed": (t, d) => `couldn't start the container of ${t} (${d.code})`,
  "container.launch_refused": (t, d) => `refused a container launch${t ? ` for ${t}` : ""} (${d.reason})`,
  "project.containers_refused": (t) => `refused to enable containers for ${t}: this host is not qualified`,
  "container.refused": (t, d) => `refused a container request${t ? ` for ${t}` : ""} (${d.code})`,
  "container.reconcile": (t, d) => `cleaned up the container of ${t} (${d.reason})`,
  "container.quarantined": (t) => `left a container it couldn't verify for ${t}`,
  "container.unknown_unit": (t) => `found a container unit it has no record of (${t})`,
};
/** Who did it: a person's name, or what acted for them. */
const actorWords = (actor: string) => actor === "@api" ? "An API token" : actor === "supervisor" ? "Rynhult"
  : actor === "acc-containers" ? "The container launcher"
  : actor.startsWith("node:") ? `Machine ${actor.slice(5)}` : actor;

export function auditWords(row: { actor: string; action: string; target: string | null; detail: Record<string, unknown> | null },
  name: (target: string) => string = (t) => t): { sentence: string; known: boolean } {
  const verb = AUDIT_VERBS[row.action];
  const target = row.target ? name(row.target) : "";
  const actor = actorWords(row.actor);
  if (verb) return { sentence: `${actor} ${verb(target, row.detail ?? {})}`.trim(), known: true };
  return { sentence: [actor, row.action.replace(/[._]/g, " "), target].filter(Boolean).join(" · "), known: false };
}

/** Rows repeated by viewing alone (terminal attaches or text reads by one person to one session on one day) fold into
    the newest. */
export function foldAudit<T extends { actor: string; action: string; target: string | null; ts: string }>(rows: T[]): (T & { times: number })[] {
  const out: (T & { times: number })[] = [];
  const seen = new Map<string, T & { times: number }>();
  for (const row of rows) {
    if (row.action !== "terminal.attach" && row.action !== "terminal.read") { out.push({ ...row, times: 1 }); continue; }
    const key = `${row.action}|${row.actor}|${row.target}|${row.ts.slice(0, 10)}`;
    const first = seen.get(key);
    if (first) { first.times += 1; continue; }
    const kept = { ...row, times: 1 };
    seen.set(key, kept);
    out.push(kept);
  }
  return out;
}

/** A profile's or run's engine settings in words (UX review CA-1, R6): Codex's sandbox and approval policy, Claude's
    permission mode. Unknown values pass through. */
const SETTING_WORDS: Record<string, Record<string, string>> = {
  sandbox: { "read-only": "Read only", "workspace-write": "Can edit the project", "danger-full-access": "Full access (no sandbox)" },
  approval: { "on-request": "Asks when it needs to", never: "Never asks", untrusted: "Asks before most commands", "on-failure": "Asks after a failure" },
  permission_mode: { default: "Asks before acting", acceptEdits: "Accept edits", plan: "Plan only", bypassPermissions: "Runs without asking", dontAsk: "Denies what isn't allowed" },
};
export const settingWords = (key: string, value: string) => SETTING_WORDS[key]?.[value] ?? value;

/** One permission summary for launch inputs and helper grants; raw inputs stay available in Details/tooltips. */
export function permissionSummary(provider: string, inputs: Record<string, unknown>): string {
  const options = (inputs.options ?? {}) as Record<string, unknown>;
  if (provider === 'codex' && inputs.sandbox && inputs.approval)
    return `${settingWords('sandbox', String(inputs.sandbox))} · ${settingWords('approval', String(inputs.approval))}`;
  if (provider === 'claude' && inputs.permission_mode) return settingWords('permission_mode', String(inputs.permission_mode));
  if (provider === 'cursor') return options.force ? 'Runs without asking' : 'Asks before tools';
  if (provider === 'copilot') return options.permissions === 'all' ? 'All permissions allowed'
    : options.permissions === 'tools' ? 'All tools allowed' : 'Asks before tools';
  return "The agent's own default";
}
