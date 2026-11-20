# claude-agent-sdk GHSA-h4mw-j7qp-8mwm (Nov 15, 2026)

**Advisory:** GHSA-h4mw-j7qp-8mwm (CVE-2026-96620), critical. claude-agent-sdk for Python before 0.2.121 passes the `resume` option to the Claude CLI without checking it. A value that starts with `-` is read as a separate flag. For example, `--mcp-config` can define a stdio MCP server, which runs a command when the CLI starts and gets around the SDK's permission controls.

**Our position:** Rynhult stays pinned at 0.1.50. Moving to 0.2.x means re-qualifying the session relay, so it is its own planned item and not part of the Nov 15 dependency batch.

**Defence in depth (until the upgrade):** `acc.claude_session_id.canonical_session_id` lets a conversation id through only if it is a canonical lowercase UUID (`str(uuid.UUID(v)) == v`, never starting with `-`). Every path that hands an id to Claude refuses anything else before it reaches the SDK or the CLI argv:

- `ClaudeAgentAdapter.start()` and `options()`, for both `resume` and the initial `session-id` (integrity failure; the client factory is never called)
- `claude_switch.terminal_resume_command`
- `providers/claude.py` launch args (`--resume` / `--session-id`). The only exception is the preview placeholder, which never runs.
- `broker/explain.py` for Claude explanations

The Agent SDK preview renders a fixed placeholder UUID and shows it back to the user as `<native-id>`. Tests are in `tests/test_session_relay_claude_session_id_guard.py`.

**When upgrading to 0.2.121 or later:** keep the guard. Re-run the relay qualification first. The adapter hard-codes `claude-agent-sdk/0.1.50` and depends on the 0.1.50 subprocess transport internals.
