"""What a session is doing now, from its tool hooks."""
from __future__ import annotations

from helpers import control, wait_for
from test_supervisor import get, launch, state_is

from acc.server.states import describe_tool


def test_describe_tool_across_clis():
    cwd = "/srv/ws"
    assert describe_tool("Bash", {"command": "npm test\necho done"}, cwd) == "Running npm test"
    assert describe_tool("Bash", {"command": "npm test"}, cwd, "done") == "Ran npm test"
    assert describe_tool("Edit", {"file_path": "/srv/ws/a.py"}, cwd, "ask") == "Asking to edit a.py"
    assert describe_tool("mcp__github__create_issue", {}, cwd, "ask") == "Asking to use github: create_issue"
    assert describe_tool("Edit", {"file_path": "/srv/ws/src/api.py"}, cwd) == "Editing src/api.py"
    assert describe_tool("view", {"path": "/srv/ws/README.md"}, cwd) == "Reading README.md"  # Copilot
    assert describe_tool("run_command", {"CommandLine": "ls -la"}, cwd) == "Running ls -la"  # Antigravity
    assert describe_tool("write_to_file", {"TargetFile": "/srv/ws/a.txt"}, cwd) == "Editing a.txt"
    assert describe_tool("Shell", {"command": "git status"}, cwd) == "Running git status"  # Cursor
    assert describe_tool("TodoWrite", {"todos": []}, cwd) == "Updating the plan"
    assert describe_tool("WebSearch", {"query": "tmux"}, cwd) == "Fetching tmux"
    assert describe_tool("mcp__github__create_issue", {"title": "x"}, cwd) == "Using github: create_issue"
    assert describe_tool("frobnicate", {}, cwd) == "Using frobnicate"
    assert len(describe_tool("Bash", {"command": "x" * 500}, cwd)) == 200


async def test_activity_follows_the_turn(api, project, env):
    session = await launch(api, project)
    await wait_for(state_is(api, session["id"], "idle"))
    control(env, session["id"], "tool")

    async def running():
        s = await get(api, session["id"])
        return s if s["activity"] == "Running npm test" and s["observed_state"] == "active" else None
    await wait_for(running)

    control(env, session["id"], "tool-done")
    done = await wait_for(state_is(api, session["id"], "idle"))
    assert done["activity"] is None  # the turn is over


async def test_a_busy_database_never_drops_a_hook(api, project, env, monkeypatch):
    """A lock while applying a hook line is retried on the next pass, not recorded as a bad line and skipped."""
    from sqlalchemy.exc import OperationalError
    session = await launch(api, project)
    await wait_for(state_is(api, session["id"], "idle"))
    supervisor = api.app.state.supervisor
    real, failed = supervisor._apply_hook, []

    def busy_once(db, s, record):
        if not failed and (record.get("hook") or {}).get("hook_event_name") == "PreToolUse":
            failed.append(1)
            raise OperationalError("INSERT", {}, Exception("database is locked"))
        return real(db, s, record)
    monkeypatch.setattr(supervisor, "_apply_hook", busy_once)
    control(env, session["id"], "tool")

    async def running():
        s = await get(api, session["id"])
        return s if s["activity"] == "Running npm test" else None
    await wait_for(running)
    events = (await api.get(f"/api/sessions/{session['id']}/events")).json()
    assert failed and not any(e["kind"] == "spool_parse_error" for e in events)
