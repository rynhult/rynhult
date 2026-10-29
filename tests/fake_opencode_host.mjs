// Loads ACC's OpenCode plugin the way OpenCode 2 does (default export {id, setup(ctx)}) and drives it from stdin, one
// JSON object per line, for tests (tests/fake_cli.py's fake opencode and tests/test_opencode.py):
//   {"event": {...}}                  an event on the plugin's stream (ctx.event.subscribe)
//   {"tool": name, "input": {...}}    a tool call: execute.before (the guard), then execute.after if it was allowed
//   {"shell": command}                OpenCode creating a shell for a command (shell create.before)
//   {"exit": true}                    unload the plugin and stop
// Answers on stdout: "ready", then "allowed" or "refused: <reason>" per tool call, "shell env: <JSON>" per shell.
// FAKE_HOST_NO_EVENTS=1 leaves out ctx.event, FAKE_HOST_NO_TOOL_HOOK=1 ctx.tool and FAKE_HOST_NO_SHELL_HOOK=1 ctx.shell (as an
// incompatible OpenCode would).
import { createInterface } from "node:readline"
import { pathToFileURL } from "node:url"

const plugin = (await import(pathToFileURL(process.argv[2]).href)).default
const session = process.argv[3] || "ses_fake"
const hooks = {}
const queue = []
let wake = null

const ctx = {
  app: { version: "2.1.3-fake" },
  location: { directory: process.cwd() },
  tool: { hook: async (name, callback) => { hooks[name] = callback; return { dispose() { delete hooks[name] } } } },
  shell: { hook: async (name, callback) => { hooks[`shell.${name}`] = callback; return { dispose() {} } } },
  event: {
    subscribe: ({ signal } = {}) => ({
      async *[Symbol.asyncIterator]() {
        while (!signal?.aborted) {
          if (queue.length) yield queue.shift()
          else await new Promise((resolve) => { wake = resolve; signal?.addEventListener("abort", resolve) })
        }
      },
    }),
  },
}
if (process.env.FAKE_HOST_NO_EVENTS) delete ctx.event
if (process.env.FAKE_HOST_NO_TOOL_HOOK) delete ctx.tool
if (process.env.FAKE_HOST_NO_SHELL_HOOK) delete ctx.shell

let cleanup
try {
  cleanup = await plugin.setup(ctx)
} catch (error) {
  console.log("setup failed: " + error.message)
  process.exit(4)
}
console.log("ready")

for await (const line of createInterface({ input: process.stdin })) {
  if (!line.trim()) continue
  const message = JSON.parse(line)
  if (message.event) {
    queue.push(message.event)
    wake?.()
  }
  if (message.tool) {
    const call = { tool: message.tool, sessionID: message.session || session, agent: "build", messageID: "msg_1",
      id: "call_1", input: message.input || {} }
    try {
      await hooks["execute.before"](call)
      console.log("allowed")
      await hooks["execute.after"]?.({ ...call, status: "completed", result: { content: [] } })
    } catch (error) {
      console.log("refused: " + error.message)
    }
  }
  if (message.shell) {
    const created = { command: message.shell, cwd: process.cwd(), timeout: 0, shell: "/bin/bash", env: { TERM: "xterm" } }
    await hooks["shell.create.before"]?.(created)
    console.log("shell env: " + JSON.stringify(created.env))
  }
  if (message.exit) break
  await new Promise((resolve) => setTimeout(resolve, 20)) // let the stream deliver before the next line
}
cleanup?.()
process.exit(0)
