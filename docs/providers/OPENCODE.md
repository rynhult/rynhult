# OpenCode as ACC's sixth provider: design

Status: approved 2026-10-22. This page is the design. The short notes that sit with the other providers are in [PROVIDERS.md](../PROVIDERS.md).

## What OpenCode is, as checked

OpenCode is an MIT-licensed coding agent. It ships one binary with a full-screen terminal UI (`opencode`), a local HTTP server (`opencode serve`) and a plugin API. Version 2 (npm `@opencode/cli`, 2.0.0 on 2026-09-11) breaks v1's plugin and server APIs.

**How this was checked:** OpenCode 2.1.3 was downloaded into a scratch folder from npm (`@opencode/cli-linux-x64`; its SHA-512 matched the registry's `integrity`). I read its `--help` output, its docs and its source at the `v2.1.3` tag (`services/www/src/docs/content`, `packages/{cli,core,plugin,schema}`). I then ran it with an isolated `HOME` against a scripted OpenAI-compatible fake model on 127.0.0.1, both with `opencode run` and with the TUI in a private tmux.

| Fact | Verified | How |
|---|---|---|
| Plain `opencode` "discovers or starts one shared background server for your user account"; `--standalone` runs a private one instead | yes | docs (`cli/index`), `--help`; the private server is `opencode serve --stdio --port 0` on 127.0.0.1 with a random password, and it ends when the TUI's pipe closes (source) |
| `--session <id>` resumes that session, or creates it with that id on the first prompt; ids must start with `ses` | yes | source (`commands/handlers/default.ts`, `schema/session-id.ts`); live: an id ACC picked was used, and a restart showed the same conversation |
| `--prompt <text>` gives a first prompt to the TUI; `--auto` approves anything not explicitly denied | yes | `--help`; live |
| `OPENCODE_CONFIG_DIR` replaces the global config folder (`opencode.json`, `AGENTS.md`, `agents/`, `skills/`, `plugins/`); `OPENCODE_CONFIG` adds one more config file; `OPENCODE_CONFIG_CONTENT` is inline config that outranks project files | yes | source (`server-process.ts`, `core/config.ts`); live |
| v2 plugins: a module whose default export is `{id, setup(ctx)}`, loaded from `<config>/plugins/*.js`. `ctx.tool.hook("execute.before")` runs before **every** tool call, including calls made inside Code Mode (`execute`) and MCP tools; throwing rejects that one call and the model sees the message | yes | docs (`build/plugins`), source (`core/tool.ts`, `plugin/promise/adapter.ts`); live: acc-guard blocked `rm -rf` outside the workspace and a `.env` read, even with `--auto` |
| The plugin event stream (`ctx.event.subscribe`) carries `session.execution.started/succeeded/failed/interrupted`, `permission.asked/replied`, `form.created/replied/cancelled` (the `question` tool) and `session.created` | yes | live. `session.status` exists in the schema but was not seen on the plugin stream in 2.1.3, so ACC doesn't rely on it |
| Permissions: ordered `{action, resource, effect}` rules, with effects `allow`/`ask`/`deny`; actions `shell`, `edit`, `read`, `external_directory`, MCP `<server>_<tool>`, … The built-in `build` agent allows most tools by default | yes | docs (`permissions`, `agents`); live: a global `shell: ask` rule made the build agent ask |
| Policies (`provider.use`, `tool.use`, `integration.use`): broader config outranks narrower, so the global config outranks a project | yes | docs (`policies`); live (below) |
| A repository's `.opencode/opencode.json` can switch a plugin off (`"plugins": ["-acc"]`, or an `integration.use` deny policy) | **yes: a real risk** | live: either switched ACC's guard off. Countered as described under "Guardrails" |
| Skills `<config>/skills/<id>/SKILL.md`; agents `<config>/agents/<id>.md` (frontmatter `description`, `mode`); MCP `mcp.servers.<name>` with `type: local` and an `environment` | yes | docs; live: skill and subagent listed to the model; a local MCP server's tool answered with the environment it was given (as `tools.<server>.<tool>` in Code Mode) |
| Saved provider accounts (API keys, OAuth) and all sessions live in one SQLite database, `~/.local/share/opencode/opencode.db` (WAL, `busy_timeout` 5 s); a provider's environment variable on the server is used without being saved | yes | docs (`cli/providers`), source (`database/database.ts`); live: `auth list` shows an `env` connection |
| 75+ providers, Ollama found automatically, free models without an account | partly | docs; `opencode models` listed free `opencode/*` models with no account. Real providers weren't tried (no accounts used) |
| Updates: `update: "disable"` in the global config and `OPENCODE_DISABLE_AUTOUPDATE=1` turn its own updater off | yes | docs (`config`), source (`services/updater.ts`) |
| Interrupt: Escape twice, a moment apart (one Escape shows "esc again to interrupt") | yes | live |

**Not verified:** sign-in with real provider accounts (GitHub Copilot device flow, ChatGPT, OpenCode Zen); OpenCode on arm64 or musl; macOS machines; long sessions with compaction; subagents in background sessions.

## Design

### Launch: one private server per session
The broker starts OpenCode in tmux as the person's agent account, like the other CLIs:

```
opencode --standalone --session ses_<id> [--auto] [--prompt <first prompt>]
```

- `--standalone` gives each ACC session its **own server process**, a child of the TUI that ends with it. The shared per-user background service is never used, so sessions never share a server, its tool processes or its event stream.
- **ACC assigns the id** (`ses_` plus OpenCode's own id shape), like Claude and Copilot. A resume passes the same `--session`, which never starts a new conversation. The broker refuses to resume unless the session is in OpenCode's database.
- **The session's own config folder:** `OPENCODE_CONFIG_DIR=<run>/opencode` replaces `~/.config/opencode`, so every session sees only what ACC compiled for it. The account's own global OpenCode config (its plugins, skills, agents and MCP servers) does not apply to ACC sessions. The project's `opencode.json` and `.opencode/` still apply, as a repository's own settings.
- `OPENCODE_DISABLE_CLAUDE_CODE=1` keeps `~/.claude/skills` out (the agent account's Claude plugins). `~/.agents/skills` can't be switched off separately, so it still applies.
- **Shared state, on purpose:** the account's database (`~/.local/share/opencode/opencode.db`) holds the conversations and the provider accounts saved with `/connect`. A database per session would lose every saved sign-in. Each private server only reads and writes its own session, and SQLite in WAL mode with a busy timeout handles several servers at once.

### State and "needs you": a real signal
The session's plugin, `<run>/opencode/plugins/acc/` (`index.js` and a `package.json`), is ACC's. It appends records to `$ACC_RUN_DIR/events.jsonl` in acc-hook's format (provider `opencode`), and `normalize_hook` turns them into ACC's vocabulary:

| Plugin record | ACC event | State |
|---|---|---|
| `acc.ready` (at load, with the session id ACC assigned) | SessionStart | idle; this also confirms the start (`start_verify="hook"`) |
| `session.execution.started` | UserPromptSubmit | working |
| `session.execution.succeeded` | Stop | idle |
| `session.execution.failed` | StopFailure | idle, plus an Inbox item |
| `session.execution.interrupted` | Interrupt | idle |
| `permission.asked` (`action`, `resources`) | PermissionRequest | **needs you** ("shell: npm test") |
| `form.created` (the agent asks a question) | Notification (`agent_needs_input`) | **needs you** |
| `permission.replied`, `form.replied`/`cancelled` | PermissionReplied | working |
| PreToolUse (written by acc-guard) / `tool.after` | PreToolUse / PostToolUse | working, "doing now" line |
| `acc.unload` | SessionEnd | (run ends) |

Only the ACC session's own top-level OpenCode session drives working and idle. A subagent's child session finishing doesn't make the parent idle. Its permission prompts and questions do raise "needs you", because they appear in the same terminal. The tier is **full**.

As a fallback, `prompt_patterns` also match OpenCode's prompts on screen ("Permission required", "Questions"), and a quiet screen still ends a turn after 120 s. If a future version changes the event names, ACC degrades to screen detection like Cursor, instead of going blind.

### Guardrails: enforced, and checked at start
The plugin registers `tool.execute.before`. For every tool call (shell, edit, write, patch, read, grep, glob, webfetch, MCP, and the calls inside Code Mode) it runs `bin/acc-guard opencode PreToolUse`, passing `{session_id, cwd, tool_name, tool_input}`, and waits for it:

- exit 0: the call goes ahead;
- exit 2: the call is rejected with acc-guard's reason (the model sees it and carries on), and the session's owner gets the usual Inbox item;
- anything else (the guard can't run, times out or crashes): the call is **rejected**, so the guard fails closed.

OpenCode's tool inputs (`command`/`workdir`, `path`, `patchText`) are the keys acc-guard already reads. Its credential database and legacy `auth.json` join acc-guard's secret files, and `opencode auth export` (which prints every saved credential) joins the commands that print secrets.

**A repository can't switch the guard off.** Two defences were each verified live against a project config holding `"plugins": ["-acc"]` and an `integration.use` deny on `plugin:*`:
1. `OPENCODE_CONFIG_CONTENT` (set in the session's environment, so tool calls can't change it) lists the plugin last **by its absolute path**, which turns it back on. It must be a path, never the bare name `acc`. OpenCode treats a name that matches no loaded plugin as an npm package, and it downloaded and ran the unrelated npm package `acc` in one experiment where the local plugin was blocked. A configured path must also be a folder, which is why the plugin is one.
2. The session's global config allows `integration.use` on the plugin's path. Global policies outrank project ones.

The plugin, the session's shell, its `opencode.json` and `guardrails.json` live in the run folder, which acc-guard already protects. Their hashes go into `ACC_GUARD_FILES`, so changing them blocks every tool call. acc-guard also refuses writes to `.opencode/plugins/` (and, as a precaution not checked against 2.1.3, `.opencode/plugin/`, `.opencode/tools/` and `.opencode/tool/`), file-tool edits to a project's `opencode.json(c)` that mention plugins or policies, and shell writes to those files, because a new plugin or custom tool is code that runs inside OpenCode's server. (Until security review #2 only the plugins folder was actually refused.)

**What if the plugin doesn't load?** For example, a future OpenCode could change the plugin API.
- **Shell commands fail closed.** The session's `shell` setting (in the inline config) is `<run>/opencode/bin/bash`. It is a short script that runs a `-c` command only when `ACC_OPENCODE_GUARDED=1` is in its environment, which the plugin sets through OpenCode's `shell create.before` hook. Without the plugin, every shell command is refused with a message saying why. The person's interactive terminal (no `-c`) starts as usual. This was verified live with a plugin that throws at load.
- **Everything else is fail-visible.** There is no `acc.ready`, so ACC never sees the session start, and after the start timeout it shows **hasn't started** in the Inbox instead of looking healthy. File tools (edit, write, read) would then run without the guard's checks; they only reach what the agent's account can reach anyway, but secrets reads wouldn't be refused.

That's why the version is pinned (below), and why a new line is checked (`tests/test_opencode_live.py`) before ACC moves to it.

**Limits, the same as for every provider:** acc-guard is a safety net, not a sandbox. Code Mode's `fetch` makes HTTP requests without a tool call, and a script the agent writes and runs is checked as a command, not line by line.

### Permission modes and agents
Two launch options in New session:

- **Permissions:** *Ask before commands* (default: `shell` and `external_directory` ask, edits allowed, as with Claude's "accept edits"); *Ask before commands and edits*; *Approve everything not denied ⚠* (`--auto`; dangerous, so the launch needs confirming as for the other CLIs). The rules go in `OPENCODE_CONFIG_CONTENT`, so a repository can't loosen the person's choice. acc-guard applies in every mode.
- **Agent:** *Build* (default) or *Plan* (read-only), as `default_agent`.

### Profiles → `<run>/opencode/`
| Profile item | Becomes |
|---|---|
| instructions (+ vault key names, + extra prompt) | `opencode/AGENTS.md` (OpenCode's global instructions file) |
| skills | `opencode/skills/<id>/…` (Markdown made safe as for every provider) |
| subagents | `opencode/agents/<id>.md` with only `description` and `mode: subagent`. Claude's `tools`/`model` keys aren't valid OpenCode fields |
| MCP servers (from the lockfile) | `opencode-mcp.json` (`OPENCODE_CONFIG`): `mcp.servers.<id>`, local servers with `environment`, remote ones with `url`. Secret values are filled in only when the file is written, and the file is removed when the session stops |

### Models and endpoints
- **Model:** New session's model field takes OpenCode's `provider/model` form (for example `anthropic/claude-sonnet-4-5`, `opencode/big-pickle`), listed by `opencode models --standalone`. It is written as `model` in the session's config. Empty means OpenCode's default.
- **Model endpoints:** an ACC endpoint becomes a custom provider, `acc`, in the session's config: `@opencode/ai/providers/openai-compatible` or `…/anthropic-compatible`, depending on the endpoint's style. Its key comes from `ACC_ENDPOINT_API_KEY` in the session's 0600 environment file, and the model is `acc/<model>`.

### Sign-in
OpenCode has no single vendor account, so ACC doesn't drive a sign-in for it. The Providers card shows **Ready** when the CLI runs, and lists the connections from `opencode auth list --format json`. There are three ways to give it models:
1. **Keys from the vault:** give the project a key with the provider's variable name (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, …). It reaches only that session's server through `session.env`, and OpenCode uses it without saving it.
2. **Model endpoints** (Ollama, LM Studio, vLLM, DeepSeek, …).
3. **Accounts** (GitHub Copilot, ChatGPT, OpenCode Zen): `/connect` once in any OpenCode session's terminal. The account is saved in the agent account's OpenCode database, and later sessions use it.

OpenCode's free models need no sign-in.

### Install and pin
acc-packages gets an `opencode` entry, using a new `npm-binary` method:

- It reads the npm registry entry for the platform package (`@opencode/cli-linux-x64`, `-baseline` on CPUs without AVX2, or `-linux-arm64`) and downloads the tarball over HTTPS. It **checks its SHA-512 against the registry's `integrity`**, then unpacks only `package/bin/opencode` into `/opt/opencode/versions/<version>/` and links `/usr/local/bin/opencode` atomically.
- **Pinned:** ACC pins a release line and a minimum version (`2.1`, at least `2.1.3`, verified here). Install and update take the newest patch of that line, and the daily auto-update stays within it. A new minor or major line needs an ACC change that re-verifies the plugin and bumps the pin.
- The binary is root-owned, so OpenCode's own updater can't replace it. ACC also switches that updater off.
- Linux only for now; Mac machines aren't offered OpenCode.

**Tolerating change:** the plugin checks which hooks are there, reads event fields defensively (`data` or `properties`), and wraps everything except the guard in try/catch. The Python side ignores records it doesn't know. The transcript reader looks up the session's table and columns, and returns nothing rather than failing.

### History and resume
`transcript()` looks the session id up in OpenCode's database, read-only (any `session*` table with an `id` column). Without that row, ACC won't resume. History search reads the session's user and assistant text from `session_message`. The database file's stamp, including its `-wal`, tells ACC when to read again.

## Tests
- Provider unit tests: options, argv (launch, resume, `--auto`, `--prompt`), environment (the session id, config folder, inline config, endpoint key), compiled files (config, plugin, AGENTS.md, skills, agents, MCP with placeholders, endpoint provider), hook normalisation, screen prompts, transcript and messages from a fixture SQLite database, `auth list` and `models` parsing.
- **The real plugin under Node:** a fake `ctx` drives `acc.js`. A blocking guard rejects the call with its reason; an allowing one passes; a missing guard fails closed. Event records are written for the main session only, and a context without an event stream still guards.
- acc-guard: OpenCode payloads (shell, `edit` with `path`, `patch` with `patchText`), the database as a secret, `opencode auth export`, `.opencode/plugins/` writes, and plugin or policy edits to a project's `opencode.json`.
- Supervisor flow with a fake `opencode` (tests/fake_cli.py): launch with an assigned id, ready → working → needs you → idle, guard records, crash and resume with the same id, refusal without a transcript.
- acc-packages: catalog entry, the version line and minimum, integrity checking (a bad checksum installs nothing), and archive layout.
- Playwright: OpenCode in New session (options, model, endpoint), launch, and its Providers card.
- An optional live test (`ACC_OPENCODE_BIN`) repeats the guard check above against a real binary and a fake model; it is skipped otherwise.
