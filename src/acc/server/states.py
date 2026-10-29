"""Pure session-state rules (plan §3, M1 findings F2, F3, F10, F11). No I/O here."""
from __future__ import annotations

import re

WAITING_NOTIFICATIONS = {"permission_prompt", "agent_needs_input"}
LIVE_STATES = {"active", "waiting_input", "idle"}


def state_from_hook(event_name: str, hook: dict) -> str | None:
    """Observed state implied by a provider hook, or None if the hook says nothing about it."""
    if event_name == "SessionStart":
        return "idle"  # up and waiting for a prompt; UserPromptSubmit follows if one was given
    if event_name in ("UserPromptSubmit", "PreToolUse", "PostToolUse", "PermissionDenied", "PermissionReplied"):
        return "active"  # a tool ran, or a prompt was answered, refused or timed out: the turn is moving again
    if event_name == "PermissionRequest":
        return "waiting_input"
    if event_name == "Notification":
        kind = hook.get("notification_type") or ""
        if kind == "idle_prompt":
            return "idle"
        if kind in WAITING_NOTIFICATIONS or kind.startswith("elicitation"):
            return "waiting_input"
        return None
    if event_name in ("Stop", "StopFailure", "Interrupt"):
        return "idle"
    return None


# Tool names across CLIs (Claude's Bash/Edit/Read, Copilot's bash/view/edit, Cursor's Shell, agy's run_command and
# write_to_file, Codex's shell/apply_patch) -> what the agent is doing: now, done, and when asking. First match wins.
TOOL_VERBS = (
    (("todo", "plan"), ("Updating the plan", "Updated the plan", "Asking to update the plan")),
    (("bash", "shell", "run_command", "exec", "terminal", "command"), ("Running", "Ran", "Asking to run")),
    (("web", "fetch", "browse", "url"), ("Fetching", "Fetched", "Asking to fetch")),
    (("edit", "write", "replace", "create", "patch", "insert", "str_replace"), ("Editing", "Edited", "Asking to edit")),
    (("read", "view", "cat", "open"), ("Reading", "Read", "Asking to read")),
    (("grep", "glob", "search", "find", "list", "ls"), ("Searching", "Searched", "Asking to search")),
    (("task", "agent"), ("Delegating", "Delegated", "Asking to delegate")),
)
USING = ("Using", "Used", "Asking to use")
TENSES = {"now": 0, "done": 1, "ask": 2}
TARGET_KEYS = ("command", "CommandLine", "cmd", "file_path", "filePath", "path", "TargetFile", "AbsolutePath",
               "notebook_path", "pattern", "query", "Query", "url", "Url", "description")


def describe_tool(tool: str, tool_input: dict | None, cwd: str | None, tense: str = "now") -> str:
    """One line for the session list: "Running npm test", "Edited src/api.py", "Asking to use github: create_issue"."""
    index = TENSES[tense]
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    target = next((str(tool_input[k]) for k in TARGET_KEYS if isinstance(tool_input.get(k), (str, int))
                   and str(tool_input[k]).strip()), "")
    target = target.strip().splitlines()[0] if target.strip() else ""
    if cwd and target.startswith(cwd.rstrip("/") + "/"):
        target = target[len(cwd.rstrip("/")) + 1:]
    if tool.startswith("mcp__"):  # mcp__<server>__<tool>
        parts = tool.split("__")
        verb, what = USING[index], f"{parts[1]}: {'__'.join(parts[2:])}" if len(parts) > 2 else tool
    else:
        words = set(re.findall(r"[a-z]+", re.sub(r"([a-z])([A-Z])", r"\1 \2", tool).lower()))  # MultiEdit, read_file
        verbs = next((forms for keys, forms in TOOL_VERBS if words & set(keys)), None)
        if verbs:
            verb, what = verbs[index], target
        else:
            verb, what = USING[index], tool + (f" ({target})" if target else "")
    return f"{verb} {what}".strip()[:200]


def is_user_exit(exit_status: int | None, exit_signal: int | None) -> bool:
    """A clean exit (Ctrl+C twice, /exit, /quit) is the owner's decision: do not resume it (F10).

    Non-zero status, a signal, or the pane vanishing (reboot, kill) is a crash: recover.
    """
    return exit_signal is None and exit_status == 0


def retry_delay(attempts_made: int, backoff: tuple[float, ...]) -> float | None:
    """Seconds to wait before the next resume attempt, or None when attempts are exhausted."""
    if attempts_made >= len(backoff):
        return None
    return backoff[attempts_made]
