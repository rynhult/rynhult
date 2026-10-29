# OpenCode sessions: a private server, ACC's plugin as the guard, a pinned line

Renumbered on 2026-10-24 from 0004 to 0027.

Owner approved OpenCode as a provider on 2026-10-22; detailed design: [OpenCode](../providers/OPENCODE.md).

## Context
By default OpenCode runs every client against one background server per user account. It reads the account's global
config and the repository's `.opencode/` settings, which can also switch plugins off. It has no hook files like the
other CLIs: guarding and observing it means code inside its server (a v2 plugin). It ships almost daily, and v2
broke v1's plugin API.

## Decision
- Every ACC session runs `opencode --standalone --session <id ACC assigned>`: its own server, and its own config
  folder (`OPENCODE_CONFIG_DIR` in the run folder) in place of the account's. The account's database stays shared,
  because it holds the conversations (needed for resume) and the provider accounts people connect.
- ACC's plugin is the guard and the state signal. It calls `bin/acc-guard` before every tool call and refuses the call
  if the guard can't answer. It reports OpenCode's own events. The inline config (environment) names it by absolute
  path and the session's global config allows it by policy, so repository settings can't remove it. The session's
  shell runs only commands the plugin marked, so a plugin that fails to load stops shell commands.
- acc-packages installs a pinned release line (2.1, at least 2.1.3), checksum-checked against the npm registry.
  Moving to a new line is an ACC change, checked with `tests/test_opencode_live.py`.

## Consequences
Sessions don't see the account's own OpenCode plugins, agents or MCP servers; models come from vault keys, endpoints
or accounts connected with `/connect`. A session that never got a prompt has nothing stored, so ACC won't resume it
(it shows recovery needed). If the plugin can't load, file tools would run unchecked: the session shows "hasn't
started", but only shell commands fail closed.

## Alternatives considered
- **The shared background service** with a session per ACC session: one process for everyone's sessions, and
  plugins and config shared between them.
- **A database per session** (`OPENCODE_DB`): full isolation, but every connected provider account would be lost.
- **Permission rules or policies as the guard:** they match patterns but can't run ACC's shell parser, and a plugin
  can't loosen a configured deny.
- **Screen detection only** (as for Cursor): OpenCode exposes real events, so there was no reason to guess.
- **Tracking `@latest`**, like the npm CLIs: too much churn for code that runs inside OpenCode's server.
