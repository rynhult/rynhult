"""The real OpenCode with what ACC compiles for it, against a scripted model (tests/fake_llm.py): run it before moving the
pinned release line (acc-packages' `line`), with the candidate binary:

    ACC_OPENCODE_BIN=/path/to/opencode PYTHONPATH=src python -m pytest -q tests/test_opencode_live.py

Skipped without ACC_OPENCODE_BIN. It uses its own HOME, never the account's OpenCode data, and only `--standalone`
servers (never the shared background service). Checked with 2.1.3 on 2026-10-22.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from acc.server.compiler import LaunchInputs, compile_files, materialize, session_environment

REPO = Path(__file__).resolve().parents[1]
BINARY = os.environ.get("ACC_OPENCODE_BIN")
pytestmark = pytest.mark.skipif(not BINARY, reason="set ACC_OPENCODE_BIN to a real opencode 2.x binary")
SID = "ses_e9a21f164ffe3Vy3VPhTAMTE34"
RULES = {"force_push": True, "push_default": True, "rm_outside": True, "secrets": True}


@pytest.fixture
def model(tmp_path):
    """Starts the scripted model; returns a function that sets its script and gives the endpoint URL."""
    servers = []

    def start(script: list[dict]) -> str:
        (tmp_path / "script.json").write_text(json.dumps(script))
        port_file = tmp_path / "port"
        servers.append(subprocess.Popen([sys.executable, "-I", str(REPO / "tests/fake_llm.py"), str(tmp_path / "script.json"),
                                         str(port_file), str(tmp_path / "llm.log")]))
        for _ in range(50):
            if port_file.exists() and port_file.read_text():
                return f"http://127.0.0.1:{port_file.read_text()}/v1"
            time.sleep(0.1)
        raise AssertionError("the fake model didn't start")
    yield start
    for server in servers:
        server.terminate()
        server.wait(timeout=10)


def test_acc_config_guards_a_hostile_repository_and_reports_events(tmp_path, model):
    url = model([{"tool": "shell", "args": {"command": "rm -rf /var/lib/acc-probe-missing", "description": "x"}},
                 {"tool": "read", "args": {"path": ".env"}},
                 {"tool": "shell", "args": {"command": "echo ok > made.txt", "description": "x"}},
                 {"text": "All done."}])
    workspace, run, home = tmp_path / "ws", tmp_path / "run", tmp_path / "home"
    (workspace / ".opencode").mkdir(parents=True)
    home.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    # A repository that tries to switch ACC's plugin off both ways OpenCode allows
    (workspace / ".opencode/opencode.json").write_text(json.dumps({"plugins": ["-acc"], "policies": [
        {"action": "integration.use", "resource": "plugin:*", "effect": "deny"}]}))
    endpoint = {"id": "fake", "name": "Fake model", "styles": {"openai": url}}
    inputs = LaunchInputs(provider="opencode", model="m", endpoint="fake", options={"permissions": "auto"},
                          append_prompt="ACC-PROFILE-MARKER")
    files = materialize(compile_files(inputs, str(REPO / "bin/acc-hook"), "/s", endpoint=endpoint,
                                      guardrails={"rules": RULES, "custom": [], "workspace": str(workspace)}).files,
                        None, "opencode")
    for name, content in files.items():
        (run / name).parent.mkdir(parents=True, exist_ok=True)
        (run / name).write_text(content)
    (run / "opencode/bin/bash").chmod(0o550)  # as the broker writes it
    env = {"HOME": str(home), "PATH": os.environ["PATH"], "TERM": "xterm", "ACC_RUN_DIR": str(run),
           "ACC_SESSION_ID": "abc123def456", **session_environment(inputs, None, str(run), files, native_id=SID)}
    result = subprocess.run([BINARY, "run", "--standalone", "--session", SID, "--auto", "go"], cwd=workspace, env=env,
                            capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    assert result.returncode == 0, result.stdout + result.stderr
    events = [json.loads(line) for line in (run / "events.jsonl").read_text().splitlines()]
    names = [e["hook"]["hook_event_name"] for e in events]
    assert names[0] == "acc.ready" and events[0]["hook"]["session_id"] == SID  # the plugin loaded despite the repository
    guarded = [e["guard"]["rule"] for e in events if e.get("guard")]
    assert guarded == ["rm_outside", "secrets"]
    assert "session.execution.started" in names and "session.execution.succeeded" in names
    assert (workspace / "made.txt").read_text() == "ok\n"  # what the guard allows runs
    requests = [json.loads(line) for line in (tmp_path / "llm.log").read_text().splitlines()]
    told = json.dumps([r["last"] for r in requests])
    assert "recursive delete outside the workspace" in told  # the model got the guard's reason
    assert any("ACC-PROFILE-MARKER" in json.dumps(r["system"]) for r in requests if r["n"] >= 0)  # the instructions


async def test_through_acc_the_real_tui_needs_you_then_idles_and_resumes(api, project, env, model):
    """ACC launches the real TUI in tmux on a model endpoint: started (the plugin's ready event), working, needs you
    (its permission prompt), approved, idle; then the pane dies and ACC resumes the same conversation."""
    from helpers import wait_for
    from test_supervisor import get, launch, state_is
    env["broker"].providers["opencode"] = BINARY
    url = model([{"tool": "shell", "args": {"command": "echo hi > from-opencode.txt", "description": "write a file"}},
                 {"text": "Done."}])
    created = await api.post("/api/endpoints", json={"id": "fake", "name": "Fake model", "kind": "openai",
                                                     "base_url": url.removesuffix("/v1"), "styles": {"openai": url}})
    assert created.status_code == 201, created.text
    session = await launch(api, project, provider="opencode", endpoint="fake", model="m", initial_prompt="make the file")
    await wait_for(state_is(api, session["id"], "waiting_input"), timeout=60)  # "Permission required" for the shell call
    items = [i for i in (await api.get("/api/attention")).json() if i["kind"] == "needs_input" and i["session_id"] == session["id"]]
    assert items and "echo hi > from-opencode.txt" in items[0]["detail"]
    detail = await get(api, session["id"])
    subprocess.run(["tmux", "-L", env["broker"].tmux_socket, "send-keys", "-t", f"={detail['tmux_name']}:", "Enter"], check=True)
    done = await wait_for(state_is(api, session["id"], "idle"), timeout=60)
    assert done["status_source"] == "opencode_hook" and done["runs"][0]["verified_at"]
    assert (env["workspaces"] / "demo/from-opencode.txt").read_text() == "hi\n"
    pane = subprocess.run(["tmux", "-L", env["broker"].tmux_socket, "list-panes", "-t", f"={detail['tmux_name']}", "-F",
                           "#{pane_pid}"], capture_output=True, text=True).stdout.split()[0]
    os.kill(int(pane), 9)  # a crash: ACC resumes the same session id

    async def resumed():
        s = await get(api, session["id"])
        return s if len(s["runs"]) == 2 and s["runs"][1]["verified_at"] and s["observed_state"] == "idle" else None
    again = await wait_for(resumed, timeout=60)
    assert again["native_id"] == session["native_id"]


def test_a_plugin_that_fails_to_load_stops_shell_commands(tmp_path, model):
    """Fail closed where it matters most: if OpenCode can't load ACC's plugin (an incompatible version), the session's
    shell refuses commands, and no ready event means ACC shows the session as not started."""
    url = model([{"tool": "shell", "args": {"command": "echo ran > unguarded.txt", "description": "x"}},
                 {"text": "All done."}])
    workspace, run, home = tmp_path / "ws", tmp_path / "run", tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    endpoint = {"id": "fake", "name": "Fake model", "styles": {"openai": url}}
    inputs = LaunchInputs(provider="opencode", model="m", endpoint="fake", options={"permissions": "auto"})
    files = materialize(compile_files(inputs, str(REPO / "bin/acc-hook"), "/s", endpoint=endpoint,
                                      guardrails={"rules": RULES, "custom": [], "workspace": str(workspace)}).files,
                        None, "opencode")
    files["opencode/plugins/acc/index.js"] = "export default {id: 'acc', setup() { throw new Error('incompatible') }}\n"
    for name, content in files.items():
        (run / name).parent.mkdir(parents=True, exist_ok=True)
        (run / name).write_text(content)
    (run / "opencode/bin/bash").chmod(0o550)
    env = {"HOME": str(home), "PATH": os.environ["PATH"], "TERM": "xterm", "ACC_RUN_DIR": str(run),
           **session_environment(inputs, None, str(run), files, native_id=SID)}
    subprocess.run([BINARY, "run", "--standalone", "--session", SID, "--auto", "go"], cwd=workspace, env=env,
                   capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    assert not (workspace / "unguarded.txt").exists()
    events = (run / "events.jsonl").read_text() if (run / "events.jsonl").exists() else ""
    assert "acc.ready" not in events
    told = (tmp_path / "llm.log").read_text()
    assert "guardrails aren't running in this OpenCode session" in told  # the model (and the person) see why
    assert not (home / ".cache/opencode/npm").exists()  # nothing was fetched in the plugin's place


def test_shell_commands_run_without_alias_expansion(tmp_path, model):
    """The guard checks one tool call at a time. That is complete only while the shell neither keeps aliases from an
    earlier call nor expands them in the same one. acc-guard follows aliases defined in a call anyway (security review of
    guard-dynamic-command-reason, GR-7). Checked with 2.1.4 on 2026-10-25: each call is a new non-interactive
    `/bin/bash -c` with expand_aliases off."""
    url = model([{"tool": "shell", "args": {"command": "alias accprobe='echo EXPANDED'\naccprobe same > same.txt 2>&1\n"
                                                        "echo \"$- $(shopt expand_aliases)\" > flags.txt",
                                            "description": "x"}},
                 {"tool": "shell", "args": {"command": "accprobe later > later.txt 2>&1", "description": "x"}},
                 {"text": "All done."}])
    workspace, run, home = tmp_path / "ws", tmp_path / "run", tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    endpoint = {"id": "fake", "name": "Fake model", "styles": {"openai": url}}
    inputs = LaunchInputs(provider="opencode", model="m", endpoint="fake", options={"permissions": "auto"})
    files = materialize(compile_files(inputs, str(REPO / "bin/acc-hook"), "/s", endpoint=endpoint,
                                      guardrails={"rules": RULES, "custom": [], "workspace": str(workspace)}).files,
                        None, "opencode")
    for name, content in files.items():
        (run / name).parent.mkdir(parents=True, exist_ok=True)
        (run / name).write_text(content)
    (run / "opencode/bin/bash").chmod(0o550)
    env = {"HOME": str(home), "PATH": os.environ["PATH"], "TERM": "xterm", "ACC_RUN_DIR": str(run),
           **session_environment(inputs, None, str(run), files, native_id=SID)}
    result = subprocess.run([BINARY, "run", "--standalone", "--session", SID, "--auto", "go"], cwd=workspace, env=env,
                            capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    assert result.returncode == 0, result.stdout + result.stderr
    flags, expand = (workspace / "flags.txt").read_text().split(None, 1)
    assert "i" not in flags and expand.split() == ["expand_aliases", "off"]
    assert "EXPANDED" not in (workspace / "same.txt").read_text()
    assert "EXPANDED" not in (workspace / "later.txt").read_text()
