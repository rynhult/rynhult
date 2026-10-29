// Rynhult's plugin for one OpenCode session (written to <run dir>/opencode/plugins/acc/index.js; see
// docs/providers/OPENCODE.md).
//
// - Guardrails: bin/acc-guard checks every tool call before it runs (tool.execute.before, which also covers the calls
//   inside Code Mode and MCP tools). Exit 0 allows it; anything else rejects it with the guard's reason, and a guard
//   that can't run rejects it too (fail closed). Shell commands also carry ACC_OPENCODE_GUARDED (shell.create.before):
//   the session's shell (opencode/bin/bash) refuses commands without it, so without this plugin nothing runs unchecked.
// - State: the session's events, appended to $ACC_RUN_DIR/events.jsonl in acc-hook's format (provider "opencode");
//   Rynhult's opencode provider turns them into its own vocabulary.
// OpenCode's v2 plugin API is new and changes often: only the guard is required; everything else is best effort.
import { appendFileSync } from "node:fs"
import { spawnSync } from "node:child_process"

@ACC_ENV_HELPER@

const GUARD = "@ACC_GUARD@" // filled in by Rynhult: the absolute path of bin/acc-guard
const GUARD_TIMEOUT_MS = 15000 // acc-guard gives up on a check after 4 s; this only catches a stuck process
const RUN_DIR = environmentValue("ACC_RUN_DIR") || ""
const MAIN = environmentValue("ACC_OPENCODE_SESSION") || "" // the OpenCode session Rynhult launched (--session)
// Events that say something about the session's state; the rest (text deltas, usage, catalog updates) are left out.
const FORWARD = new Set([
  "session.execution.started", "session.execution.succeeded", "session.execution.failed",
  "session.execution.interrupted", "permission.asked", "permission.replied", "form.created", "form.replied",
  "form.cancelled",
])
const MAIN_ONLY = new Set(["session.execution.started", "session.execution.succeeded", "session.execution.failed",
  "session.execution.interrupted"]) // a subagent's own session finishing doesn't end the turn

function short(value, limit = 2000) {
  if (typeof value === "string") return value.length > limit ? value.slice(0, limit) + "…" : value
  if (Array.isArray(value)) return value.slice(0, 50).map((v) => short(v, limit))
  if (value && typeof value === "object") {
    const out = {}
    for (const [key, item] of Object.entries(value).slice(0, 50)) out[key] = short(item, limit)
    return out
  }
  return value
}

function record(name, payload) {
  if (!RUN_DIR) return
  const line = JSON.stringify({ ts: new Date().toISOString(), provider: "opencode", event: name.replace(/[^A-Za-z]/g, ""),
    acc_session: environmentValue("ACC_SESSION_ID") || "", hook: { hook_event_name: name, ...payload } })
  try {
    appendFileSync(RUN_DIR + "/events.jsonl", line + "\n")
  } catch {}
}

function guard(cwd, event) {
  const payload = JSON.stringify({ hook_event_name: "PreToolUse", session_id: event.sessionID, cwd,
    tool_name: event.tool, tool_input: event.input })
  const result = spawnSync(GUARD, ["opencode", "PreToolUse"], { input: payload, encoding: "utf8", timeout: GUARD_TIMEOUT_MS })
  if (result.error || result.signal) {
    throw new Error(`Rynhult's guardrails couldn't check this tool call, so it is refused (${result.error?.message || result.signal})`)
  }
  if (result.status === 0) return
  const reason = (result.stderr || "").trim().split("\n").pop()
  throw new Error(reason || `Rynhult's guardrails refused this tool call (status ${result.status})`)
}

export default {
  id: "acc",
  async setup(ctx) {
    if (typeof ctx?.tool?.hook !== "function") {
      // Without the pre-tool hook there are no guardrails: don't report "ready", so Rynhult shows the session as not started.
      record("acc.unsupported", { session_id: MAIN, version: ctx?.app?.version, reason: "no tool.execute.before hook" })
      throw new Error("Rynhult's plugin needs OpenCode's tool.execute.before hook (OpenCode 2)")
    }
    const cwd = ctx.location?.directory || process.cwd()
    await ctx.tool.hook("execute.before", (event) => guard(cwd, event))
    if (typeof ctx.shell?.hook === "function") {
      await ctx.shell.hook("create.before", (event) => {
        if (event.env) { event.env.ACC_OPENCODE_GUARDED = "1"; event.env.RYNHULT_OPENCODE_GUARDED = "1" }
        else event.env = { ACC_OPENCODE_GUARDED: "1", RYNHULT_OPENCODE_GUARDED: "1" }
      })
    } else {
      record("acc.shell_unmarked", { session_id: MAIN, reason: "no shell.create.before hook: shell commands are refused" })
    }
    const cleanups = []
    try {
      const after = await ctx.tool.hook("execute.after", (event) => {
        if (MAIN && event.sessionID !== MAIN) return
        record("tool.after", { session_id: event.sessionID, cwd, tool_name: event.tool, tool_input: short(event.input),
          status: event.status })
      })
      if (after?.dispose) cleanups.push(() => after.dispose())
    } catch {}
    let events = false
    if (typeof ctx.event?.subscribe === "function") {
      const controller = new AbortController()
      cleanups.push(() => controller.abort())
      events = true
      void (async () => {
        for await (const event of ctx.event.subscribe({ signal: controller.signal })) {
          if (!FORWARD.has(event?.type)) continue
          const data = event.data ?? event.properties ?? {}
          const session = data.sessionID ?? data.form?.sessionID
          if (MAIN && MAIN_ONLY.has(event.type) && session !== MAIN) continue
          record(event.type, { ...short(data), session_id: session })
        }
      })().catch((error) => {
        if (!controller.signal.aborted) record("acc.events_lost", { session_id: MAIN, error: String(error).slice(0, 300) })
      })
    }
    record("acc.ready", { session_id: MAIN, cwd, version: ctx.app?.version, events })
    return () => {
      record("acc.unload", { session_id: MAIN })
      for (const cleanup of cleanups) {
        try { cleanup() } catch {}
      }
    }
  },
}
