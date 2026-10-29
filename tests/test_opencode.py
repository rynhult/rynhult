"""OpenCode (`opencode` 2.x): launch, config, Rynhult's plugin (guard and events), state, resume and history.

The fake `opencode` (tests/fake_cli.py) follows what 2.1.3 does (docs/providers/OPENCODE.md) and loads the real plugin
Rynhult compiles, under Node (tests/fake_opencode_host.mjs), so the guard and the events in these tests are the plugin's own.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest
from helpers import control, wait_for, write_db
from test_supervisor import get, launch, state_is

from acc import providers
from acc.broker import ops
from acc.providers import transcripts
from acc.server import ingest
from acc.server.compiler import LaunchInputs, build_args, compile_files, materialize, session_environment

REPO = Path(__file__).resolve().parents[1]
HOST = REPO / "tests/fake_opencode_host.mjs"
HOOK = str(REPO / "bin/acc-hook")
OC = providers.OPENCODE
SID = "ses_e9a21f164ffe3Vy3VPhTAMTE34"
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js (runs Rynhult's OpenCode plugin)")


def compiled(**inputs):
    inputs = LaunchInputs(provider="opencode", **inputs)
    return inputs, compile_files(inputs, HOOK, "/s").files


# ---------------------------------------------------------------------------------------------- launch and config


def test_launch_resume_and_options():
    inputs, files = compiled()
    for resume in (False, True):  # one flag starts or resumes the session Rynhult assigned; always a private server
        assert build_args(inputs, run_dir="/r", tmux_name="t", native_id=SID, resume=resume, files=files) == \
            ["--standalone", "--session", SID]
    auto, files = compiled(options={"permissions": "auto"}, initial_prompt="fix the build")
    assert build_args(auto, run_dir="/r", tmux_name="t", native_id=SID, resume=False, files=files) == \
        ["--standalone", "--session", SID, "--auto", "--prompt", "fix the build"]
    assert "--prompt" not in build_args(auto, run_dir="/r", tmux_name="t", native_id=SID, resume=True, files=files)
    assert OC.dangerous(auto) and not OC.dangerous(inputs)
    with pytest.raises(providers.CompileError, match="pre-assigned"):
        build_args(inputs, run_dir="/r", tmux_name="t", native_id=None, resume=False, files=files)


def test_session_config_and_environment():
    inputs, files = compiled(model="anthropic/claude-sonnet-4-5", options={"agent": "plan", "permissions": "all"})
    config = json.loads(files["opencode/opencode.json"])
    assert config["model"] == "anthropic/claude-sonnet-4-5" and config["default_agent"] == "plan"
    assert config["update"] == "disable"  # acc-packages updates it, never OpenCode itself
    # A repository can deny plugins by policy; the session's global policy (which outranks a project's) keeps Rynhult's
    assert config["policies"] == [{"action": "integration.use", "resource": "plugin:{env:ACC_RUN_DIR}/opencode/plugins/acc",
                                   "effect": "allow"}]
    plugin = files["opencode/plugins/acc/index.js"]
    assert f'const GUARD = "{REPO}/bin/acc-guard"' in plugin and "@ACC_GUARD@" not in plugin
    env = session_environment(inputs, None, "/r", files, native_id=SID)
    assert env["OPENCODE_CONFIG_DIR"] == "/r/opencode" and env["ACC_OPENCODE_SESSION"] == SID
    assert env["OPENCODE_DISABLE_AUTOUPDATE"] == "1" and env["OPENCODE_DISABLE_CLAUDE_CODE"] == "1"
    assert "OPENCODE_CONFIG" not in env  # no MCP servers, no extra file
    inline = json.loads(env["OPENCODE_CONFIG_CONTENT"])  # outranks the project's opencode.json
    # a project's "-acc" can't switch the guard off; by path, never a bare name (OpenCode would fetch npm's "acc")
    assert inline["plugins"] == ["/r/opencode/plugins/acc"] and inline["shell"] == "/r/opencode/bin/bash"
    assert {(r["action"], r["effect"]) for r in inline["permissions"]} == {("shell", "ask"), ("edit", "ask"),
                                                                          ("external_directory", "ask")}
    default = json.loads(session_environment(compiled()[0], None, "/r", files)["OPENCODE_CONFIG_CONTENT"])
    assert {r["action"] for r in default["permissions"]} == {"shell", "external_directory"}  # edits allowed
    auto = json.loads(session_environment(compiled(options={"permissions": "auto"})[0], None, "/r", files)["OPENCODE_CONFIG_CONTENT"])
    assert auto["permissions"] == []
    assert "default_agent" not in json.loads(compiled()[1]["opencode/opencode.json"])


def test_models_are_provider_slash_model():
    with pytest.raises(providers.CompileError, match="provider/model"):
        compiled(model="claude-sonnet")
    compiled(model="openrouter/anthropic/claude-sonnet-4")  # OpenRouter's own ids have a slash too


@pytest.mark.parametrize("style, package", [("openai", "@opencode/ai/providers/openai-compatible"),
                                            ("anthropic", "@opencode/ai/providers/anthropic-compatible")])
def test_model_endpoint_becomes_the_acc_provider(style, package):
    endpoint = {"id": "local", "name": "Local Ollama", "context_tokens": 32768, "styles": {style: "http://127.0.0.1:11434/v1"}}
    inputs = LaunchInputs(provider="opencode", model="qwen3:4b", endpoint="local")
    files = compile_files(inputs, HOOK, "/s", endpoint=endpoint).files
    config = json.loads(materialize(files, None, "opencode")["opencode/opencode.json"])
    assert config["model"] == "acc/qwen3:4b"
    assert config["providers"]["acc"] == {
        "name": "Local Ollama (Rynhult model endpoint)", "env": ["ACC_ENDPOINT_API_KEY"], "package": package,
        "settings": {"baseURL": "http://127.0.0.1:11434/v1"},
        "models": {"qwen3:4b": {"name": "qwen3:4b", "limit": {"context": 32768}}}}
    assert "providers" not in json.loads(files["opencode/opencode.json"])  # added when written, from endpoint.json
    assert session_environment(inputs, None, "/r", files, "sk-x")["ACC_ENDPOINT_API_KEY"] == "sk-x"
    assert session_environment(inputs, None, "/r", files)["ACC_ENDPOINT_API_KEY"] == "acc-local-endpoint"


def test_vault_context_joins_the_instructions():
    _, files = compiled(append_prompt="Be brief.")
    with_keys = OC.add_context(files, "Keys: OPENROUTER_API_KEY")
    assert with_keys["opencode/AGENTS.md"] == files["opencode/AGENTS.md"].rstrip() + "\n\nKeys: OPENROUTER_API_KEY\n"
    assert with_keys["opencode/AGENTS.md"].startswith("Rynhult mandatory core")
    assert OC.add_context(compiled()[1], "Keys: X")["opencode/AGENTS.md"] == compiled()[1]["opencode/AGENTS.md"].rstrip() + "\n\nKeys: X\n"


def test_assigned_ids_have_opencodes_shape_and_pass_acc_checks():
    ids = {OC.new_native_id() for _ in range(50)}
    assert len(ids) == 50
    for native_id in ids:
        assert re.fullmatch(r"ses_[0-9a-f]{12}[0-9A-Za-z]{14}", native_id)
        assert ops.NATIVE_ID.match(native_id) and ingest.NATIVE_ID.fullmatch(native_id)
    assert not ops.NATIVE_ID.match("ses_../../etc") and not ops.NATIVE_ID.match("ses_*")


# ---------------------------------------------------------------------------------------------- events and screen


@pytest.mark.parametrize("record, expected", [
    ({"hook_event_name": "acc.ready", "session_id": SID}, {"hook_event_name": "SessionStart", "session_id": SID}),
    ({"hook_event_name": "session.execution.started", "session_id": SID}, {"hook_event_name": "UserPromptSubmit"}),
    ({"hook_event_name": "session.execution.succeeded", "sessionID": SID}, {"hook_event_name": "Stop", "session_id": SID}),
    ({"hook_event_name": "session.execution.failed", "error": {"type": "api", "message": "rate limited"}},
     {"hook_event_name": "StopFailure", "error": "rate limited"}),
    ({"hook_event_name": "session.execution.interrupted", "reason": "user"}, {"hook_event_name": "Interrupt"}),
    ({"hook_event_name": "permission.asked", "action": "shell", "resources": ["npm test"]},
     {"hook_event_name": "PermissionRequest", "tool_name": "shell", "tool_input": {"command": "npm test"}}),
    ({"hook_event_name": "permission.asked", "action": "edit", "resources": ["src/a.ts", "src/b.ts"]},
     {"tool_name": "edit", "tool_input": {"file_path": "src/a.ts, src/b.ts"}}),
    ({"hook_event_name": "permission.replied", "reply": "once"}, {"hook_event_name": "PermissionReplied"}),
    ({"hook_event_name": "form.created", "form": {"sessionID": SID, "fields": [{"title": "Which database?"}]}},
     {"hook_event_name": "Notification", "notification_type": "agent_needs_input", "message": "asks: Which database?"}),
    ({"hook_event_name": "form.cancelled"}, {"hook_event_name": "PermissionReplied"}),
    ({"hook_event_name": "tool.after", "tool_name": "shell", "tool_input": {"command": "ls"}},
     {"hook_event_name": "PostToolUse", "tool_name": "shell"}),
    ({"hook_event_name": "acc.unload"}, {"hook_event_name": "SessionEnd"}),
    ({"hook_event_name": "PreToolUse", "tool_name": "shell"}, {"hook_event_name": "PreToolUse"}),
    ({"hook_event_name": "something.new", "x": 1}, {"hook_event_name": "something.new"}),  # tolerated, no state
])
def test_events_in_accs_vocabulary(record, expected):
    out = OC.normalize_hook(record)
    assert {k: out.get(k) for k in expected} == expected


def test_states_follow_the_events():
    from acc.server.states import state_from_hook
    names = lambda name, **f: state_from_hook(OC.normalize_hook({"hook_event_name": name, **f})["hook_event_name"],
                                              OC.normalize_hook({"hook_event_name": name, **f}))
    assert names("acc.ready") == "idle" and names("session.execution.started") == "active"
    assert names("permission.asked", action="shell", resources=["ls"]) == "waiting_input"
    assert names("form.created", form={}) == "waiting_input" and names("permission.replied") == "active"
    assert names("session.execution.succeeded") == "idle" and names("session.execution.interrupted") == "idle"


def test_approval_prompts_on_screen():
    permission = ("  ┃  △ Permission required\n  ┃\n  ┃  $ rm -rf build\n  ┃\n"
                  "  ┃   Allow once   Always allow   Reject                ctrl+f fullscreen  ⇆ select  enter confirm\n")
    assert OC.screen_prompt(permission) == "rm -rf build"
    question = "  ┃  Which one?\n  ┃  1. Alpha\n  ┃  3. Type your own answer\n  ┃  ↑↓ select  enter submit  esc dismiss\n"
    assert OC.screen_prompt(question) == ""
    answered = "  ┃  # Questions\n  ┃  Which one?\n  ┃  Alpha\n     fine\n     Build · M · 6.6s\n"
    assert OC.screen_prompt(answered) is None  # a question in the conversation isn't one being asked


def test_interrupt_presses_escape_twice(monkeypatch):
    sent = []
    monkeypatch.setattr(ops, "tmux", lambda config, *args, check=False: sent.append(args[-1]))
    monkeypatch.setattr(ops.time, "sleep", lambda s: sent.append(f"wait {s}"))
    assert ops.interrupt(None, "acc-x-1", "opencode") == {"sent": "Escape Escape"}
    assert sent == ["Escape", "wait 0.3", "Escape"]
    sent.clear()
    ops.interrupt(None, "acc-x-1", "claude")
    ops.interrupt(None, "acc-x-1")  # an older main Rynhult names no provider
    assert sent == ["Escape", "Escape"]


# ---------------------------------------------------------------------------------------------- history and resume


def database(home: Path, rows=(), sessions=(SID,), table="session_v2") -> Path:
    path = home / ".local/share/opencode/opencode.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute(f"CREATE TABLE {table} (id text PRIMARY KEY, directory text NOT NULL, parent_id text)")
        db.execute("CREATE TABLE session_message (id text PRIMARY KEY, session_id text NOT NULL, type text NOT NULL, "
                   "seq integer NOT NULL, data text NOT NULL)")
        for sid in sessions:
            db.execute(f"INSERT INTO {table} VALUES (?, '/w', NULL)", (sid,))
        for i, (sid, kind, data) in enumerate(rows):
            db.execute("INSERT INTO session_message VALUES (?, ?, ?, ?, ?)", (f"m{i}", sid, kind, i, json.dumps(data)))
    return path


def test_transcript_is_the_session_row_and_history_reads_its_messages(tmp_path, monkeypatch):
    monkeypatch.delenv('XDG_DATA_HOME', raising=False)  # explicit temporary homes below define the provider store
    other = "ses_otherotherother000001"
    path = database(tmp_path, sessions=(SID, other), rows=[
        (SID, "user", {"time": {"created": 1792709635111}, "text": "fix the billing webhook", "files": []}),
        (SID, "assistant", {"content": [{"type": "tool", "name": "shell"}, {"type": "text", "text": "Fixed it."}]}),
        (SID, "idle", {"outcome": "succeeded"}),
        (other, "user", {"text": "someone else's session"}),
    ])
    assert OC.transcript(tmp_path, SID) == path and OC.transcript(tmp_path, "ses_missing00000000000") is None
    messages = OC.session_messages(OC.messages_source(tmp_path, SID), SID)
    assert [(m["role"], m["text"]) for m in messages] == [("user", "fix the billing webhook"), ("assistant", "Fixed it.")]
    assert messages[0]["ts"].startswith("2026-")
    result = ops.transcript_messages("opencode", SID, None)  # the broker's own path, as the agent account (HOME)
    assert result == {"found": False}  # HOME is not tmp_path here: nothing found, nothing raised


def test_unknown_schema_and_odd_files_are_not_read(tmp_path, monkeypatch):
    monkeypatch.delenv('XDG_DATA_HOME', raising=False)  # explicit temporary homes below define the provider store
    database(tmp_path, table="sessions_v9")  # a renamed table that still looks like sessions: still found
    assert OC.transcript(tmp_path, SID) is not None
    other = tmp_path / "b"
    path = other / ".local/share/opencode/opencode.db"
    path.parent.mkdir(parents=True)
    path.write_text("not a database")
    assert OC.transcript(other, SID) is None and transcripts.opencode(path, SID) == []
    fifo = tmp_path / "c/.local/share/opencode/opencode.db"
    fifo.parent.mkdir(parents=True)
    os.mkfifo(fifo)  # never opened (it would block)
    assert OC.transcript(tmp_path / "c", SID) is None
    link = tmp_path / "d/.local/share/opencode/opencode.db"
    link.parent.mkdir(parents=True)
    link.symlink_to(database(tmp_path / "e"))
    assert OC.transcript(tmp_path / "d", SID) is None


# ---------------------------------------------------------------------------------------------- the plugin (Node)


def host(tmp_path, lines, guard=None, extra_env=None, rules=True):
    """Runs Rynhult's compiled plugin in the fake host; returns (exit code, host output, events written)."""
    run = tmp_path / "run"
    (run / "opencode/plugins").mkdir(parents=True, exist_ok=True)
    source = providers.opencode.plugin_source(HOOK)
    if guard:
        source = source.replace(f"{REPO}/bin/acc-guard", guard)
    (run / "opencode/plugins/acc/index.js").parent.mkdir(parents=True, exist_ok=True)
    (run / "opencode/plugins/acc/index.js").write_text(source)
    if rules:
        (run / "guardrails.json").write_text(json.dumps({"rules": {"force_push": True, "push_default": True,
                                                                   "rm_outside": True, "secrets": True},
                                                         "custom": [], "workspace": str(tmp_path / "ws")}))
    (tmp_path / "ws").mkdir(exist_ok=True)
    env = {"PATH": os.environ["PATH"], "ACC_RUN_DIR": str(run), "ACC_SESSION_ID": "abc123def456",
           "ACC_OPENCODE_SESSION": SID, **(extra_env or {})}
    result = subprocess.run(["node", str(HOST), str(run / "opencode/plugins/acc/index.js"), SID],
                            input="".join(json.dumps(line) + "\n" for line in lines), text=True, capture_output=True,
                            env=env, cwd=tmp_path / "ws", timeout=60)
    events = [json.loads(line) for line in (run / "events.jsonl").read_text().splitlines()] \
        if (run / "events.jsonl").exists() else []
    return result.returncode, result.stdout, events


@needs_node
def test_plugin_runs_the_guard_before_every_tool_call(tmp_path):
    code, out, events = host(tmp_path, [
        {"tool": "shell", "input": {"command": "rm -rf /var/lib/somewhere"}},
        {"tool": "read", "input": {"path": ".env"}},
        {"tool": "patch", "input": {"patchText": "*** Begin Patch\n*** Update File: a.txt\n*** Move to: .env\n*** End Patch"}},
        {"tool": "shell", "input": {"command": "npm test"}},
        {"exit": True}])
    assert code == 0, out
    answers = [line for line in out.splitlines() if line.startswith(("allowed", "refused"))]
    assert answers[0] == "refused: recursive delete outside the workspace (/var/lib/somewhere) is blocked by Rynhult's guardrails"
    assert answers[1].startswith("refused: .env looks like a secret") and answers[2].startswith("refused: .env looks like")
    assert answers[3] == "allowed"
    kinds = [e["hook"]["hook_event_name"] for e in events]
    assert kinds == ["acc.ready", "PreToolUse", "PreToolUse", "PreToolUse", "PreToolUse", "tool.after", "acc.unload"]
    assert events[1]["guard"]["rule"] == "rm_outside" and events[1]["provider"] == "opencode"
    assert events[0]["hook"] == {"hook_event_name": "acc.ready", "session_id": SID, "cwd": str(tmp_path / "ws"),
                                 "version": "2.1.3-fake", "events": True}
    assert events[5]["hook"]["tool_input"] == {"command": "npm test"} and events[5]["acc_session"] == "abc123def456"


@needs_node
def test_plugin_fails_closed_when_the_guard_cannot_run(tmp_path):
    _, out, _ = host(tmp_path, [{"tool": "shell", "input": {"command": "ls"}}, {"exit": True}],
                     guard=str(tmp_path / "missing-acc-guard"))
    assert "refused: Rynhult's guardrails couldn't check this tool call, so it is refused" in out
    broken = tmp_path / "broken-guard"
    broken.write_text("#!/bin/sh\ncat >/dev/null\necho 'Traceback: boom' >&2\nexit 1\n")
    broken.chmod(0o755)
    _, out, _ = host(tmp_path, [{"tool": "shell", "input": {"command": "ls"}}, {"exit": True}], guard=str(broken))
    assert "refused: Traceback: boom" in out


@needs_node
def test_plugin_honours_a_tampered_policy(tmp_path):
    """The guard's own rule: launch files changed since launch block every call (ACC_GUARD_FILES)."""
    _, out, _ = host(tmp_path, [{"tool": "shell", "input": {"command": "ls"}}, {"exit": True}],
                     extra_env={"ACC_GUARD_FILES": "guardrails.json=0000"})
    assert "refused: this session's guardrails (guardrails.json) are missing or were changed" in out


@needs_node
def test_plugin_marks_the_shells_it_guards(tmp_path):
    _, out, _ = host(tmp_path, [{"shell": "npm test"}, {"exit": True}])
    assert 'shell env: {"TERM":"xterm","ACC_OPENCODE_GUARDED":"1","RYNHULT_OPENCODE_GUARDED":"1"}' in out
    _, out, events = host(tmp_path / "x", [{"shell": "npm test"}, {"exit": True}], extra_env={"FAKE_HOST_NO_SHELL_HOOK": "1"})
    assert 'shell env: {"TERM":"xterm"}' in out  # unmarked: the session's shell will refuse it, and Rynhult is told
    assert [e["hook"]["hook_event_name"] for e in events][:2] == ["acc.shell_unmarked", "acc.ready"]


def test_the_sessions_shell_runs_only_marked_commands(tmp_path):
    shell = tmp_path / "bash"
    shell.write_text(providers.opencode.SHELL_SCRIPT)
    shell.chmod(0o755)
    base = {"PATH": os.environ["PATH"]}
    refused = subprocess.run([str(shell), "-c", "echo ran"], capture_output=True, text=True, env=base)
    assert refused.returncode == 126 and refused.stdout == "" and "guardrails aren't running" in refused.stderr
    marked = subprocess.run([str(shell), "-c", "echo ran"], capture_output=True, text=True,
                            env={**base, "ACC_OPENCODE_GUARDED": "1"})
    assert marked.returncode == 0 and marked.stdout == "ran\n"
    interactive = subprocess.run([str(shell), "-l"], input="echo hi\n", capture_output=True, text=True, env=base)
    assert interactive.stdout.strip().endswith("hi")  # the person's own terminal isn't a tool call


@needs_node
def test_plugin_forwards_the_main_sessions_events_only(tmp_path):
    child = "ses_child0000000000000001"
    _, _, events = host(tmp_path, [
        {"event": {"type": "session.execution.started", "data": {"sessionID": SID}}},
        {"event": {"type": "session.execution.succeeded", "data": {"sessionID": child}}},  # a subagent: dropped
        {"event": {"type": "permission.asked", "data": {"id": "per_1", "sessionID": child, "action": "shell",
                                                         "resources": ["npm test"]}}},  # but its prompts count
        {"event": {"type": "session.text.delta", "data": {"sessionID": SID, "delta": "x"}}},  # noise: dropped
        {"event": {"type": "form.created", "properties": {"form": {"sessionID": SID, "fields": []}}}},  # older shape
        {"event": {"type": "session.execution.succeeded", "data": {"sessionID": SID}}},
        {"exit": True}])
    seen = [(e["hook"]["hook_event_name"], e["hook"].get("session_id")) for e in events]
    assert seen == [("acc.ready", SID), ("session.execution.started", SID), ("permission.asked", child),
                    ("form.created", SID), ("session.execution.succeeded", SID), ("acc.unload", SID)]


@needs_node
def test_plugin_without_events_still_guards_and_without_tool_hooks_never_reports_ready(tmp_path):
    _, out, events = host(tmp_path, [{"tool": "shell", "input": {"command": "rm -rf /opt"}}, {"exit": True}],
                          extra_env={"FAKE_HOST_NO_EVENTS": "1"})
    assert "refused: recursive delete outside the workspace" in out and events[0]["hook"]["events"] is False
    code, out, events = host(tmp_path / "x", [{"exit": True}], extra_env={"FAKE_HOST_NO_TOOL_HOOK": "1"})
    assert code == 4 and "setup failed" in out
    assert [e["hook"]["hook_event_name"] for e in events] == ["acc.unsupported"]  # no "ready": Rynhult flags the start


# ---------------------------------------------------------------------------------------------- through Rynhult


async def test_listed_with_options_and_no_driven_login(api):
    info = next(p for p in (await api.get("/api/providers/registry")).json()["providers"] if p["id"] == "opencode")
    assert info["installed"] and info["preview"] and info["tier"] == "full" and info["login"] is None
    assert "vault" in info["login_hint"] and {"endpoints", "mcp", "skills", "agents", "needs_you"} <= set(info["supports"])
    assert info["model_format"] == "provider/model" and "2.1.3" in info["preview_note"]
    assert [o["key"] for o in info["options"]] == ["permissions", "agent"]
    assert info["options"][0]["dangerous"] == ["auto"]


async def test_preview_and_dangerous_launch(api, project):
    base = {"project_id": project["id"], "provider": "opencode", "name": "o"}
    preview = (await api.post("/api/sessions/preview", json={**base, "model": "opencode/big-pickle"})).json()
    assert preview["command"] == ["opencode", "--standalone", "--session", "ses_<session-id>"]
    assert json.loads(preview["files"]["opencode/opencode.json"])["model"] == "opencode/big-pickle"
    assert (await api.post("/api/sessions", json={**base, "options": {"permissions": "auto"}})).status_code == 409
    bad = await api.post("/api/sessions/preview", json={**base, "model": "gpt-5"})
    assert bad.status_code == 422 and "provider/model" in bad.text


@needs_node
async def test_session_runs_guarded_reports_state_and_resumes(api, project, env):
    session = await launch(api, project, provider="opencode")
    assert re.fullmatch(r"ses_[0-9a-f]{12}[0-9A-Za-z]{14}", session["native_id"])  # assigned up front
    ready = await wait_for(state_is(api, session["id"], "idle"))
    assert ready["status_source"] == "opencode_hook" and ready["runs"][0]["verified_at"]  # verified by the plugin
    run = env["run_dir"] / session["id"]
    assert oct((run / "opencode/plugins/acc/index.js").stat().st_mode)[-3:] == "440"  # read-only, its hash in ACC_GUARD_FILES
    assert oct((run / "opencode/bin/bash").stat().st_mode)[-3:] == "550"  # the session's shell
    spool = (run / "events.jsonl").read_text()
    assert '"acc.ready"' in spool

    control(env, session["id"], "turn")
    await wait_for(state_is(api, session["id"], "active"))
    await wait_for(state_is(api, session["id"], "idle"))

    control(env, session["id"], "busy")
    await wait_for(state_is(api, session["id"], "active"))
    control(env, session["id"], "subagent-done")  # a subagent finishing doesn't end the turn
    control(env, session["id"], "permission")
    waiting = await wait_for(state_is(api, session["id"], "waiting_input"))
    assert waiting["status_source"] == "opencode_hook"
    items = [i for i in (await api.get("/api/attention")).json() if i["kind"] == "needs_input" and i["session_id"] == session["id"]]
    assert items and items[0]["detail"] == "shell: npm test"
    control(env, session["id"], "permission-reply")
    await wait_for(state_is(api, session["id"], "active"))
    control(env, session["id"], "question")
    asked = await wait_for(state_is(api, session["id"], "waiting_input"))
    assert asked["status_source"] == "opencode_hook"

    control(env, session["id"], "guarded git push origin main")

    async def blocked():
        found = [i for i in (await api.get("/api/attention")).json()
                 if i["kind"] == "guardrail_blocked" and i["session_id"] == session["id"]]
        return found[0] if found else None
    item = await wait_for(blocked)
    assert "git push origin main" in item["title"] and "main/master" in item["detail"]

    control(env, session["id"], "say please migrate the billing webhooks")
    await wait_for(state_is(api, session["id"], "idle"))
    # OpenCode stores a session with its first prompt: the first look (at launch) found nothing and backs off a minute
    api.app.state.supervisor.__dict__.get("_history_retry", {}).clear()

    async def found():
        results = (await api.get("/api/search", params={"q": "billing webhooks"})).json()["results"]
        return results or None
    assert (await wait_for(found))[0]["session"]["id"] == session["id"]

    control(env, session["id"], "crash")

    async def resumed():
        s = await get(api, session["id"])
        return s if len(s["runs"]) == 2 and s["runs"][1]["verified_at"] and s["observed_state"] == "idle" else None
    again = await wait_for(resumed, timeout=30)
    assert again["native_id"] == session["native_id"]
    pane = subprocess.run(["tmux", "-L", env["broker"].tmux_socket, "capture-pane", "-p", "-t", f"={again['tmux_name']}:"],
                          capture_output=True, text=True).stdout
    assert "resumed · --standalone · plugin found" in pane


@needs_node
async def test_never_prompted_session_is_not_resumed_into_a_new_one(api, project, env):
    session = await launch(api, project, provider="opencode")
    await wait_for(state_is(api, session["id"], "idle"))
    control(env, session["id"], "crash")  # OpenCode stores a session only once it has a prompt
    stuck = await wait_for(state_is(api, session["id"], "recovery_needed"), timeout=30)
    assert "wasn't found on the machine" in stuck["last_error"]


async def test_providers_card_health_and_models(api, env, monkeypatch):
    (env["home"] / ".local/share/opencode").mkdir(parents=True)
    (env["home"] / ".local/share/opencode/fake-copilot").write_text("ok")
    health = (await api.get("/api/accounts?refresh=1")).json()["opencode"]
    assert health["logged_in"] and health["providers"] == ["GitHub Copilot"] and "plan" not in health
    assert health["version"] == "2.1.3-fake"
    models = (await api.get("/api/models?refresh=1")).json()["opencode"]
    assert [m["id"] for m in models] == ["opencode/big-pickle", "opencode/fake-free"]


def test_a_cli_without_its_own_sign_in_is_not_responding_rather_than_logged_out(api):
    from acc.server import attention
    from acc.server.models import AttentionItem
    with write_db(api.app.state.db) as s:
        attention.evaluate_auth(s, {"opencode": {"logged_in": True}}, "fresh-agent")
        attention.evaluate_auth(s, {"opencode": {"logged_in": False, "error": "auth list failed (1)"}}, "fresh-agent")
        s.commit()
        items = [(i.title, i.detail) for i in s.query(AttentionItem).filter(AttentionItem.kind == "auth")]
    assert items == [("OpenCode isn't responding for fresh-agent", "auth list failed (1). See the Providers page.")]
