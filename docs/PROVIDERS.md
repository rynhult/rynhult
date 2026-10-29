# More agent providers and local models: plan

Status: in progress. All steps shipped 2026-10-21: registry, Copilot CLI (verified live), model endpoints, the packages helper, Antigravity and Cursor (previews until verified with real accounts). OpenCode followed on 2026-10-22 (step 7, preview). Gemini CLI was added, then dropped the same day: Google retired it in favour of Antigravity CLI. the owner asked for the top providers and local LLMs, focusing on the common ones with native CLIs when scope gets large. Research: CLI docs and package registries checked 2026-10-21 (versions below).

## Principle
ACC stays "native CLI in tmux, supervised through the CLI's own hooks". A provider is worth adding when its CLI has an interactive terminal UI, hooks (or at least start/stop signals), resume by id, and a headless login. Tools that are web UIs or lack sessions (DeepSeek Harness, Aider) are out of scope. Model breadth comes from **model endpoints** (below), not from more CLIs.

## Providers, by fit
| Provider | CLI (version checked) | Hooks for state | Resume | Headless login | Local/compatible endpoints | Tier |
|---|---|---|---|---|---|---|
| Claude Code | `claude` (apt) | full (incl. permission, notification) | `--resume <id>` | paste code | Anthropic-compatible (`ANTHROPIC_BASE_URL`) | full (today) |
| Codex | `codex` 0.163.0 | most (static `~/.codex/hooks.json`) | `resume <id>` | device code | OpenAI-compatible (`model_providers`, `--oss`) | full (today) |
| GitHub Copilot CLI | `copilot` 1.0.103 (npm `@github/copilot`, Node 22+) | full: sessionStart/End, UserPromptSubmit, Stop, notification, permissionRequest; `sessionId` in every payload; user hooks in `~/.copilot/hooks/*.json` | `--resume=<id>` | `copilot login --device-code`, or token | BYOK: `COPILOT_PROVIDER_BASE_URL`, `COPILOT_PROVIDER_TYPE` openai/anthropic, `COPILOT_MODEL` | **full** |
| ~~Gemini CLI~~ | retired by Google, replaced by Antigravity CLI | dropped 2026-10-21 | | | | |
| Antigravity CLI | `agy` (install script) | partial: SessionStart, Stop, tool events; no permission/notification/end | `--conversation <id>` (third-party docs) | paste code; keyring storage may need a headless keyring | no | standard (preview) |
| Cursor CLI | `agent` (install script) | partial in the CLI (sessionStart, stop, tool events; reported unreliable on Linux) | `--resume <id>` | `NO_OPEN_BROWSER=1 agent login`, or API key | no | basic (preview) |
| OpenCode | `opencode` 2.1.3 (npm `@opencode/cli`, platform binary) | full, from ACC's own plugin: tool hooks plus the server's event stream (turns, permission prompts, questions) | `--session <id>` (ACC assigns it) | none driven by ACC: vault keys, endpoints, or `/connect` in a session | OpenAI- and Anthropic-compatible (a custom `acc` provider) | full (preview) |

"Waiting for you" detection needs a permission/notification hook. Where a CLI lacks it (Antigravity, Cursor), the provider lists its approval prompt's text (`prompt_patterns`): each reconcile, the broker checks the bottom of the pane and returns only what is being asked (e.g. the command), and ACC shows "needs you" (source `screen`) until the prompt is gone. Verified live on both 2026-10-21.

## Pre-tool hooks and guardrails (2026-10-21)
Every CLI's pre-tool hook runs `bin/acc-guard <provider> PreToolUse` (installed in /srv/acc/bin next to acc-hook). It records the event for the "doing now" line and enforces the session's `guardrails.json` (Settings → Guardrails). The broker writes `guardrails.json` and the run folder's hook settings read-only and puts their hashes in `ACC_GUARD_FILES` (the CLI's own environment); a missing, broken or changed policy blocks every tool call until a restart. Deny formats, each verified live by blocking `rm -rf ../probe` with `ls` allowed:

| CLI | Hook | Deny | Allow | Notes |
|---|---|---|---|---|
| Claude Code | `PreToolUse` in the session's settings.json | exit 2, reason on stderr (JSON decision on stdout too) | exit 0, no output | |
| Codex | `PreToolUse`/`PostToolUse` added to ~/.codex/hooks.json by bootstrap | exit 2, reason on stderr | exit 0, no output | Bootstrap records trust for ACC's exact command groups on reviewed versions 0.163.0 and 0.164.0; other hooks and unknown versions/config formats retain manual review. “Hooks need review” becomes Needs you with a plain explanation. `-c hooks.*` session hooks are ignored. Algorithm and versioned upstream citations: [hook trust research](research/codex-hook-trust.md). |
| Copilot CLI | `PreToolUse` in ~/.copilot/hooks/acc.json | `{"permissionDecision":"deny","permissionDecisionReason":…}`, exit 0 | exit 0, no output | Exit 2 also denies but the agent only sees "hook exited with code 2". |
| Cursor CLI | `preToolUse` in ~/.cursor/hooks.json | `{"permission":"deny","user_message":…,"agent_message":…}` | no output | |
| Antigravity | `PreToolUse` (matcher group) in ~/.gemini/config/hooks.json | `{"decision":"deny","reason":…}` | **no output** | `{}` denies the call (seen live); its docs say `decision` is required. |
| OpenCode | ACC's plugin (`tool.execute.before`) in the session's config folder runs acc-guard | exit 2, reason on stderr: the plugin throws it, and the model sees it | exit 0 | Any other result (guard missing, crashed, timed out) refuses the call. Verified live with 2.1.3 and a fake model, also with `--auto` and inside Code Mode. |

## Model endpoints (local LLMs, DeepSeek and other compatible APIs)
Owners register endpoints in ACC: a name, a base URL, the API style (OpenAI-compatible and/or Anthropic Messages), an optional key (stored as a secret, injected only at launch), and how to list models.
- **Local:** Ollama (0.40.7: OpenAI `/v1` and Anthropic `/v1/messages`; models from `/api/tags`), LM Studio, llama.cpp server, vLLM.
- **Hosted:** DeepSeek (`https://api.deepseek.com` OpenAI-style, `/anthropic` Anthropic-style), OpenRouter, others.
- **Which CLIs can use them:** Claude Code (Anthropic style: `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_MODEL`), Codex (OpenAI style: a `model_providers` entry via `-c`), Copilot CLI (BYOK, either style). The New session dialog shows "Model: <endpoint> / <model>" for those agents; hooks, resume and the terminal work exactly as with the vendor's own models.
- `setup.sh` offers to install Ollama (optional); the Host page shows endpoint health.

## Architecture
1. **Provider registry** (`src/acc/providers/`): one module per provider implementing a common interface:
   - identity: id, label, colour, executable, install hint, tier and capability flags (permission signal, resume, usage, remote control, endpoints);
   - launch: option schema (served to the UI), validation and the "dangerous launch" rule, compiled files, argv for first launch and resume, env, session-id policy (pre-assigned or learned from SessionStart), start-verification policy;
   - hooks: how they're installed (per-session file, or a static per-account file written by `acc-agent-add`) and a normaliser from the CLI's event names and fields to ACC's canonical events;
   - broker side: transcript locator, workspace trust writer, auth status, login argv and flow type (paste code or device code), model listing, usage source;
   - account bootstrap (hooks file, onboarding flags).
   Claude and Codex move onto it first with no change in behaviour; the ~40 hard-coded `claude`/`codex` branches go away.
2. **API and UI driven by the registry:** `GET /api/providers` returns each provider's metadata, option schema and whether its CLI is installed for the account. The New session dialog, Providers page, badges, filters, catalog profile defaults and login flows render from it. Profiles get optional per-provider sections; missing ones use the provider's defaults.
3. **Installation:** each provider's CLI is optional. `setup.sh` asks which to install; `install.sh` installs the chosen ones at pinned versions (npm or vendor script) and `acc-agent-add` bootstraps each account's hooks.
4. **Testing:** `tests/fake_cli.py` emulates each provider's documented flags, hook payloads, resume and login output, so the whole flow runs in CI without accounts. Providers not yet exercised against the real CLI are labelled **preview** in the UI until someone verifies them (a short checklist per provider in this file).

## Steps (one PR each)
1. Provider registry; Claude and Codex ported; `/api/providers`; UI and catalog data-driven. No behaviour change; all tests pass.
2. GitHub Copilot CLI (full tier), with device-code login and BYOK.
3. Model endpoints: registry, secrets, model listing, Claude/Codex/Copilot wiring, optional Ollama in setup, Host page health.
4. Antigravity CLI (Gemini CLI was added here, then dropped: retired by Google).
5. Cursor CLI (basic).
6. Docs: wiki pages (providers, endpoints), README, Guide screenshots.
7. OpenCode (2026-10-22): a private server and config folder per session, guarded through ACC's plugin ([providers/OPENCODE.md](providers/OPENCODE.md)).

## Not now
Qwen Code, Kimi, Mistral Vibe, Aider, Amazon Q/Kiro: revisit if the owner wants them (Qwen Code is the likeliest next). OpenCode was added on 2026-10-22 (below). DeepSeek Harness is a web UI, not a terminal agent.

## Verification checklist (per provider, once someone has an account)
Launch → SessionStart hook seen → prompt → working → permission prompt shows "Needs you" (where supported) → Stop → idle → kill the pane → ACC resumes the same conversation → login flow from Providers → models list → MCP server from the catalog visible in the CLI.

## Copilot CLI notes (step 2)
- Checked against `copilot --help`, `help environment`, `help config`, `login --help` and `instruction list` (1.0.102; the pin moved to 1.0.103, the version installed and used on the ACC host, on 2026-10-23) without an account: `--session-id <uuid>` starts or resumes, so ids are assigned up front; `--no-auto-update` keeps the pinned version; instructions go in `<run>/instructions/AGENTS.md` via `COPILOT_CUSTOM_INSTRUCTIONS_DIRS` (verified it is picked up); skills via `--add-dir <run>/copilot` (`.github/skills`); MCP via `--additional-mcp-config @<run>/mcp.json`; folder trust via `copilot config trustedFolders`.
- Hooks: `~/.copilot/hooks/acc.json`, written by `acc-agent-add`, passes the event name to `acc-hook` (`acc-hook copilot <Event>`), so camelCase-only events (`notification`, `errorOccurred`) are named even if their payload isn't.
- Verified live 2026-10-21 (BYOK on Ollama, and the owner's own login): hooks and payloads, `--session-id`, session-state layout, trust in `config.json`, `storeTokenPlaintext` for headless login, SessionStart on the first prompt. Still unverified: the login-state file (`~/.copilot/config.json` `last_logged_in_user`; the CLI may use the system keyring instead), where session state lives (ACC looks for anything under `~/.copilot` named after the session id), first-run screens in tmux, and the `notification_type` values Copilot sends.

## Antigravity CLI notes (step 4)
- From `agy --help` (1.4.4) and Google's hooks reference: `--conversation <id>` resumes (ids learned from SessionStart's `conversationId`), `--mode accept-edits|auto|plan`, `--dangerously-skip-permissions`, `--model`, `--effort`; hooks in `~/.gemini/config/hooks.json` as a named group (ACC's is `acc`; others are kept), events SessionStart / PreInvocation / PostToolUse / Stop (`fullyIdle` false mid-turn). No permission/notification/end events: tier "standard", 90 s quiet-screen fallback.
- Sign-in: a one-line prompt (`agy -p "Reply with OK."`) prints a Google URL and reads the pasted code (60 s), but only when stdin is a terminal: with a pipe it says "Run 'agy' to log in". ACC's paste flow gives it a pseudo-terminal (`login_tty`). `agy models` never starts a sign-in. Without a desktop Secret Service, tokens go to `~/.config/antigravity/plugin-secrets.json`.
- Installed by acc-packages from Google's release manifest (SHA-512 checked) to `/usr/local/bin/agy` (root-owned, so its self-update can't replace it; ACC's daily check does).
- Catalog items (verified live 2026-10-22, `docs/reviews/skills-by-provider.md`): `--add-dir <run>/antigravity` makes that folder a customization root, so its plugin `.agents/plugins/acc/` (`plugin.json`, `rules/AGENTS.md`, `skills/`, `agents/`, `mcp_config.json`) loads for that session only, at launch and with `--conversation`.

- **Smoke test 2026-10-21 (free "Antigravity Starter Quota", Gemini 3.8 Flash):** a real ACC session wrote hello.txt; working → idle on agy's own hooks, conversation id learned. Found and fixed: hooks were written as `matcher` groups for every event, which agy loads but never runs (only PreToolUse/PostToolUse take groups; its built-in hooks reference, in the binary); handlers must print a JSON object; the folder trust prompt is now pre-answered (`trustedWorkspaces`).

## Cursor CLI notes (step 5)
- From `cursor-agent --help`, `status` and `login` (2026.10.19): `--trust` skips the folder prompt, `--mode plan|ask`, `--force` (dangerous), `--model`, `--resume <chatId>` (ids learned from hooks' `conversation_id`). Hooks in `~/.cursor/hooks.json` (version 1; ACC's entries recognised by their command, others kept): sessionStart, beforeSubmitPrompt, postToolUse, stop (status), sessionEnd. No permission event: tier standard, 90 s quiet-screen fallback.
- Sign-in "link": with `NO_OPEN_BROWSER=1`, `login` prints a cursor.com link and completes once approved (no code). `status` reports it.
- **Smoke test 2026-10-21 (free tier):** a real ACC session wrote hello.txt; sessionStart/beforeSubmitPrompt/stop hooks drove idle → working → idle, chat id learned. A shell-command approval prompt shows as idle (quiet-screen fallback), as expected for the standard tier.
- Catalog items (verified live 2026-10-22, `docs/reviews/skills-by-provider.md`): `--plugin-dir <run>/cursor/acc` (`.cursor-plugin/plugin.json`, `skills/`, `agents/`, `.mcp.json`); the instructions come from the plugin's sessionStart hook (`additional_context`), because the CLI doesn't apply plugin rules. MCP tool calls ask "Run this MCP tool?", matched as needing the user.
- acc-packages installs the release package to `/opt/cursor-agent/versions/<version>` and links `/usr/local/bin/cursor-agent` (Cursor publishes no checksum; downloads need a non-default User-Agent).

## OpenCode notes (step 7, 2026-10-22)
Design and what was verified: [providers/OPENCODE.md](providers/OPENCODE.md). In short:
- `opencode --standalone --session ses_<id> [--auto] [--prompt …]`: a private server for each session, never the account's shared background service. ACC assigns the id in OpenCode's own shape. A resume passes the same id, and ACC refuses it when the session isn't in OpenCode's database (OpenCode stores a session only once it has a prompt).
- The session's config folder (`OPENCODE_CONFIG_DIR=<run>/opencode`) holds `opencode.json` (model, primary agent, `update: disable`, a policy allowing ACC's plugin), `AGENTS.md`, `skills/`, `agents/`, `plugins/acc/` and `bin/bash` (the session's shell). MCP servers are in `opencode-mcp.json` (`OPENCODE_CONFIG`; secrets, removed at stop). `OPENCODE_CONFIG_CONTENT` carries the permission rules, the shell and the plugin's path, so a repository can't loosen the person's choice or switch the guard off. It must be a path: a bare plugin name OpenCode can't match is fetched from npm.
- If the plugin fails to load, the session's shell refuses commands, because only the plugin marks them (`ACC_OPENCODE_GUARDED`). With no `acc.ready`, ACC shows **hasn't started**.
- State comes from the plugin: `acc.ready` (SessionStart; it confirms the guard loaded), `session.execution.*` (working, idle, failed, interrupted), `permission.asked`/`form.created` ("needs you"), and the tool hooks. Prompts on screen and a 120 s quiet screen are the fallback, in case a future version renames its events.
- Interrupt is two Escapes (`interrupt_keys`). History reads `session_message` from OpenCode's database, read-only.
- acc-packages method `npm-binary`: the newest 2.1.x (at least 2.1.3) platform binary from npm, with its SHA-512 checked against the registry's `integrity`, into `/opt/opencode/versions/<v>` with `/usr/local/bin/opencode` linked to it.
- **Still to verify with real accounts:** provider sign-ins (`/connect`: Copilot device flow, ChatGPT, Zen), real models' tool calls, arm64.

## Gemini CLI (dropped)
- Added as a preview (API-key sign-in), then removed on 2026-10-21: Google retired Gemini CLI and moved users to Antigravity CLI (developers.googleblog.com, "Transitioning Gemini CLI to Antigravity CLI"). acc-packages no longer offers it; the provider, its key sign-in and its tests were deleted.


## CLI version and update policy

The application locks do not lock separately installed agent CLIs. The executable
reported by the broker is the runtime provenance; an administrator can still
replace a CLI outside ACC. **Providers → Agent software** reports installed
versions, the ACC install target and upstream availability in the package note.

| Provider | ACC-managed install/update target | Automatic channel |
|---|---|---|
| Codex | Exactly 0.163.0, recorded in the compatibility table above | Pinned; a new ACC catalog pin is required to advance |
| Copilot | Exactly 1.0.103; preview qualification limits above still apply | Pinned; a new ACC catalog pin is required to advance |
| OpenCode | Newest stable 2.1.x, minimum 2.1.3 | Supported line; patches may advance, new lines require ACC qualification |
| Claude Code | Candidate from the configured signed apt repository; without apt (Fedora, openSUSE), Anthropic's native release 2.1.308, SHA-256 from Anthropic's manifest | Rolling upstream, owner opt-in; apt remains held between helper actions. The native release is pinned: a new version is a Rynhult change |
| Antigravity / Cursor | Vendor manifest / release package | Rolling upstream, owner opt-in |
| Ollama | Vendor installer | Rolling upstream, owner opt-in |

Linux/WSL helpers and Mac npm installs use the same catalog pins. For pinned npm
CLIs, Check and the daily timer query upstream availability but never install
`@latest`; the reported `latest` target stays at the ACC pin, with
`upstream_latest`, channel and check time in diagnostics. Install/Update selects
the exact target. Daily updates never downgrade a newer manually installed CLI.
A manual Install/Update can restore the ACC pin.

Rolling channels start with auto-update off. On the first refresh after this policy
arrives, legacy implicit rolling auto-update values reset to off; the owner can
explicitly turn them on again. Manual Update uses the current vendor release.
This opt-in accepts compatibility changes independent of an ACC release. Mac
packages have no daily automatic update channel.

Before advancing a pin or supported line, run the provider checklist above
(hooks, permission refusal, resume, transcript ingestion, models and usage), record
the exact CLI and qualification limits here, change the catalog, and run fake-CLI
and wire fault regressions. A help-only check does not establish paid-account
compatibility. No new live-provider qualification was performed for this policy.
