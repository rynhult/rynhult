import { expect, test } from "@playwright/test";
import { byRecent, groupInbox, groupSessions, needsYou } from "../src/lib/order";
import { publishFactsLine, publishWarnings } from "../src/lib/publish";
import { auditWords, eventWords, foldAudit, settingWords, sourceWords } from "../src/lib/words";
import { advancedSummary, suggestedName } from "../src/components/newSession";
import { feedbackMessage } from "../src/lib/feedback";
import type { ObservedState } from "../src/lib/types";
import { demoSessions } from "./helpers";
import { ENV_VARIABLE_GROUPS, ENV_VARIABLE_PATTERN, ENV_VARIABLES } from "../src/lib/envVariables";
import { getChunkFailure, lazyImport, subscribeToChunkFailure } from "../src/lib/lazyImport";

// Pure functions from web/src, run by Playwright without a browser (the project has no separate unit runner).

for (const message of [
  "Failed to fetch dynamically imported module: https://example.com/assets/page.js",
  "error loading dynamically imported module: https://example.com/assets/page.js",
  "Importing a module script failed.",
]) {
  test(`lazy imports recover once from ${message}`, async () => {
    let attempts = 0;
    const result = await lazyImport(async () => {
      if (++attempts === 1) throw new TypeError(message);
      return { value: 42 };
    });
    expect(result).toEqual({ value: 42 });
    expect(attempts).toBe(2);
  });
}

test("module evaluation bugs still reject, including after a network retry", async () => {
  for (const failsFirst of [false, true]) {
    let attempts = 0;
    const fault = new Error("Module initialization bug");
    await expect(lazyImport(async () => {
      if (++attempts === 1 && failsFirst) throw new TypeError("Failed to fetch dynamically imported module");
      throw fault;
    })).rejects.toBe(fault);
    expect(attempts).toBe(failsFirst ? 2 : 1);
  }
});

test("an exhausted import publishes reload state and remains suspended rather than discarding drafts", async () => {
  let attempts = 0;
  const fault = new TypeError("Failed to fetch dynamically imported module");
  let unsubscribe = () => {};
  const notice = new Promise<Error | null>((resolve) => {
    unsubscribe = subscribeToChunkFailure(() => resolve(getChunkFailure()));
  });
  const originalError = console.error;
  let diagnostics = 0;
  console.error = (...args: unknown[]) => {
    if (args[0] === "App module failed to load after one retry" && args[1] === fault) diagnostics++;
    else originalError(...args);
  };
  try {
    const pending = lazyImport(async () => { attempts++; throw fault; });
    expect(await notice).toBe(fault);
    expect(attempts).toBe(2);
    expect(diagnostics).toBe(1); // Assert the synthetic diagnostic without polluting the gate log.
    expect(await Promise.race([
      pending.then(() => "resolved", () => "rejected"),
      new Promise<string>((resolve) => setTimeout(() => resolve("suspended"), 20)),
    ])).toBe("suspended");
  } finally {
    unsubscribe();
    console.error = originalError;
  }
});

test("suggested environment names are unique, valid, and described in provider groups", () => {
  const names = ENV_VARIABLES.map((v) => v.name);
  expect(names.length).toBeGreaterThan(0);
  expect(new Set(names).size).toBe(names.length);
  for (const group of ENV_VARIABLE_GROUPS) {
    expect(group.provider.trim()).not.toBe("");
    for (const variable of group.variables) {
      expect(variable.name).toMatch(ENV_VARIABLE_PATTERN);
      expect(variable.description.trim()).not.toBe("");
    }
  }
  for (const valid of ["_TOKEN", "APP_KEY_2", "A".repeat(64)]) expect(valid).toMatch(ENV_VARIABLE_PATTERN);
  for (const invalid of ["", "1TOKEN", "lowercase", "BAD-NAME", "A".repeat(65)]) expect(invalid).not.toMatch(ENV_VARIABLE_PATTERN);
});

const sessions = () => demoSessions() as unknown as { id: string; name: string; observed_state: ObservedState; team_id?: string | null; last_output_at: string }[];

test("sessions group Needs you → Working → Idle → Stopped, newest first within a group", () => {
  const groups = groupSessions(sessions());
  expect(groups.map((g) => g.label)).toEqual(["Needs you", "Working", "Idle", "Stopped"]);
  expect(groups.flatMap((g) => g.sessions.map((s) => s.name))).toEqual(
    ["api-refactor", "checkout-flow", "ui-polish", "notes-cleanup", "go-no-go", "market-scan", "old-spike"]);
  expect(groups.map((g) => g.count)).toEqual([1, 2, 3, 1]);
  expect(groupSessions(sessions(), ["needs_you"]).map((g) => g.label)).toEqual(["Needs you"]);
});

test("recent order ignores state", () => {
  expect(byRecent(sessions()).map((s) => s.name).slice(0, 3)).toEqual(["notes-cleanup", "go-no-go", "market-scan"]);
});

test("a team sits with its most urgent member, and only blocked sessions count as Needs you", () => {
  const team = sessions().map((s) => (s.name === "ui-polish" || s.name === "api-refactor" ? { ...s, team_id: "t1" } : s));
  const [first] = groupSessions(team);
  expect(first.label).toBe("Needs you");
  expect(first.sessions.map((s) => s.name).sort()).toEqual(["api-refactor", "ui-polish"]);
  expect(first.count).toBe(1);
  expect(needsYou(team).map((s) => s.name)).toEqual(["api-refactor"]);
  const recovery = sessions().map((s) => (s.name === "go-no-go" ? { ...s, observed_state: "recovery_needed" as ObservedState } : s));
  expect(needsYou(recovery)).toHaveLength(2);
});

test("inbox items group waiting sessions, approvals, alerts, then notes", () => {
  const items = [
    { id: 1, kind: "guardrail_blocked", severity: "info" as const },
    { id: 2, kind: "publish_request", severity: "warn" as const },
    { id: 3, kind: "ci_failed", severity: "critical" as const },
    { id: 4, kind: "needs_input", severity: "warn" as const },
    { id: 5, kind: "tool_request", severity: "warn" as const },
  ];
  const groups = groupInbox(items);
  expect(groups.map((g) => g.label)).toEqual(["Waiting sessions", "Approvals", "Alerts", "Notes"]);
  expect(groups.map((g) => g.items.map((i) => i.id))).toEqual([[4], [2, 5], [3], [1]]);
});

test("inbox sources read as words; the raw value is kept for anything unknown", () => {
  const label = (id: string) => ({ claude: "Claude Code" }[id] ?? id);
  expect(sourceWords("claude_hook", label)).toBe("Claude Code");
  expect(sourceWords("agent")).toBe("Agent request");
  expect(sourceWords("session ui-polish")).toBe("Agent request");
  expect(sourceWords("guardrails")).toBe("Guardrail");
  expect(sourceWords("psutil")).toBe("Host monitor");
  expect(sourceWords("codex login status")).toBe("Sign-in check");
  expect(sourceWords("alex")).toBe("alex");
});

test("publish warnings: unreserved port, someone else's port, public, and a replaced site first", () => {
  const request = { host: "preview.example.com", port: 8010, access: "internal" as const, action: "publish" as const, replaces: null };
  expect(publishWarnings({ ...request, port_owner: null }).map((w) => w.text)).toEqual(["Port 8010 isn't reserved for any project."]);
  expect(publishWarnings({ ...request })).toEqual([]);  // port owner not known: no guess
  expect(publishWarnings({ ...request, port_owner: { project: "demo-web", label: null }, project: "demo-web" })).toEqual([]);
  expect(publishWarnings({ ...request, port_owner: { project: "billing", label: null }, project: "demo-web" })[0].text)
    .toBe("Port 8010 is reserved for project billing, not demo-web.");
  const risky = publishWarnings({ ...request, access: "public", port_owner: null,
    replaces: { id: "p0", port: 8020, access: "public", project: "shop", decided_by: "alex" } });
  expect(risky.map((w) => w.tone)).toEqual(["bad", "wait", "wait"]);
  expect(risky[0].text).toBe("Replaces the live site: now port 8020 (project shop), on the public internet.");
  expect(publishWarnings({ ...request, action: "unpublish", port_owner: null })).toEqual([]);
  expect(publishFactsLine({ ...request, port_owner: null, requested_by: "alex", project: "demo-web" }))
    .toBe("Requested by alex · project demo-web · port 8010 · private networks only");
});

test("New session names a session from its task, and the Advanced line says every choice", () => {
  expect(suggestedName("Fix the login form's error message")).toBe("fix-the-login-form");
  expect(suggestedName("  ")).toBe("session");
  expect(suggestedName("!!!", "researcher")).toBe("researcher");
  expect(advancedSummary({ run: "once", skills: 1 })).toBe("Once · default model · shared checkout · 1 skill");
  expect(advancedSummary({ run: "once", model: "Opus", effort: "high", options: ["accept edits"], worktree: true, skills: 2, mcps: 1, goal: true, autoresume: true }))
    .toBe("Once · Opus, high effort · accept edits · own worktree · 2 skills, 1 MCP · goal set · resumes after limits");
  expect(advancedSummary({ run: "several", runs: 3 })).toBe("3 agents, each in its own worktree");
  expect(advancedSummary({ run: "helpers", skills: 0 })).toBe("With helpers · default model · own worktree · no skills");
  expect(advancedSummary({ run: "schedule", schedule: "daily at 09:00, paused", worktree: true, skills: 1 }))
    .toBe("On a schedule (daily at 09:00, paused) · default model · own worktree · 1 skill");
  expect(advancedSummary({ run: "once", workspace: true, skills: 1, name: "market-scan" })).toBe("market-scan, once · default model · 1 skill");
});

// Words, not engine identifiers (UX review CO-1, SE-10, AU-1, CA-1, R6).
test("event, audit and setting words", () => {
  expect(eventWords({ kind: "SessionStart", payload: {} })).toBe("Session started");
  expect(eventWords({ kind: "PreToolUse", payload: { tool_name: "Bash" } })).toBe("Using a tool: Bash");
  expect(eventWords({ kind: "run_launch", payload: {} })).toBe("Started");
  expect(eventWords({ kind: "state", payload: { from: "idle", to: "waiting_input" } }, (s) => (s === "waiting_input" ? "Needs you" : "Idle")))
    .toBe("Now Needs you (was Idle)");
  expect(eventWords({ kind: "something_new", payload: {} })).toBe("Something new");
  const row = { actor: "alex", action: "terminal.attach", target: "a1", detail: { mode: "write", take: false } };
  expect(auditWords(row, () => "api-refactor").sentence).toBe("alex opened api-refactor's terminal to type");
  expect(auditWords({ ...row, action: "project.trust", target: "demo-api", detail: null }).sentence).toBe("alex trusted demo-api");
  expect(auditWords({ ...row, action: "odd.thing_done", detail: null })).toEqual({ sentence: "alex · odd thing done · a1", known: false });
  expect(auditWords({ ...row, actor: "@api", action: "node.add", target: "alex-pc", detail: null }).sentence).toBe("An API token added machine alex-pc");
  expect(auditWords({ ...row, actor: "node:alex-mac", action: "node.enroll", target: "alex-mac", detail: null }).sentence).toBe("Machine alex-mac joined as a machine");
  const ts = "2026-10-23T10:00:00Z";
  const folded = foldAudit([{ ...row, ts }, { ...row, ts }, { ...row, action: "session.create", ts }, { ...row, ts }, { ...row, target: "b2", ts }]);
  expect(folded.map((r) => [r.action, r.target, r.times])).toEqual([
    ["terminal.attach", "a1", 3], ["session.create", "a1", 1], ["terminal.attach", "b2", 1]]);
  expect(settingWords("sandbox", "workspace-write")).toBe("Can edit the project");
  expect(settingWords("approval", "never")).toBe("Never asks");
  expect(settingWords("permission_mode", "bypassPermissions")).toBe("Runs without asking");
});

test("terminal text reads are audited in words and fold like attaches", () => {
  const ts = "2026-10-24T09:00:00Z";
  const row = { actor: "alex", action: "terminal.read", target: "a1", detail: null, ts };
  expect(auditWords(row).sentence).toBe("alex read a1's terminal output as text");
  const folded = foldAudit([row, row, { ...row, action: "terminal.attach", detail: { mode: "read" } }]);
  expect(folded.map((r) => [r.action, r.times])).toEqual([["terminal.read", 2], ["terminal.attach", 1]]);
});

test("publishing address audit events read as owner actions", () => {
  const row = { actor: "alex", target: "publishing.addresses", detail: { label: "Example edge", address: "192.0.2.42" } };
  expect(auditWords({ ...row, action: "publish.address_add" }).sentence).toBe("alex added publishing address Example edge (192.0.2.42)");
  expect(auditWords({ ...row, action: "publish.address_remove" }).sentence).toBe("alex removed publishing address Example edge (192.0.2.42)");
  expect(auditWords({ ...row, action: "publish.address_default" }).sentence).toBe("alex made Example edge the default publishing address");
});

test("issue-server audit decisions name the server in plain language", () => {
  const added = auditWords({ actor: "alex", action: "issue_server.add", target: "issue_server.demo",
    detail: { base_url: "https://git.example.lan" } });
  expect(added).toEqual({ known: true, sentence: "alex allowed git server https://git.example.lan" });
  const confirmed = auditWords({ actor: "alex", action: "issue_server.confirm", target: "issue_server.demo",
    detail: { base_url: "https://git.example.lan" } });
  expect(confirmed).toEqual({ known: true, sentence: "alex re-confirmed addresses for https://git.example.lan" });
  expect(auditWords({ actor: "alex", action: "issue_server.remove", target: "issue_server.demo", detail: {} }))
    .toEqual({ known: true, sentence: "alex removed an allowed git server" });
});

test('device audit actions explain preparation and pending policy in plain language', () => {
  const row = { actor: 'alex', target: 'random-device-id', detail: { person: 'casey' } };
  for (const [action, sentence] of [
    ['device.pair', 'alex paired a browser for casey (policy not applied)'],
    ['device.rename', 'alex renamed a paired browser'],
    ['device.renew', 'alex renewed a browser for casey (policy not applied)'],
    ['device.revoke', 'alex revoked a browser identity for casey (policy not applied)'],
    ['device.remove', 'alex removed a paired browser record for casey'],
    ['device.pairing_mismatch', 'alex cancelled pairing for casey: the matching code differed'],
    ['remote_access.prepare', 'alex saved LAN pairing settings'],
    ['remote_access.apply', 'alex applied and verified public access'],
    ['remote_access.off', 'alex removed public access routing'],
    ['remote_access.failed', 'alex could not verify public access; routing removal requested'],
  ]) expect(auditWords({ ...row, action })).toEqual({ known: true, sentence });
});

test("review feedback quotes whole characters of a long code line", () => {
  // 159 ASCII characters, then emoji: the 160th character is an emoji, and cutting by UTF-16 units split it in two.
  const code = "x".repeat(159) + "😀".repeat(3);
  const message = feedbackMessage([{ path: "app.py", line: 2, code, text: "why?" }]);
  expect(message).toContain("`" + "x".repeat(159) + "😀` — why?");
  expect(message.isWellFormed()).toBe(true);
});
