"""Explicit, account-scoped walkthroughs of the diff ACC actually displays."""
import asyncio
import json
import os
import runpy
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from helpers import wait_for, write_db
from sqlalchemy import select
from test_changes import repo
from test_supervisor import launch

from acc.server.models import Observation, Session, Setting

DIFF = {"git": True, "files": 1, "list": [
    {"path": "app.py", "status": "M", "additions": 1, "deletions": 1, "binary": False}],
    "diff": "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n one\n-two\n+TWO\n",
    "truncated": False}


def walkthrough(refs=None):
    return json.dumps({"steps": [{"title": "Update the response", "summary": "Uppercase the second line.",
                                  "references": refs if refs is not None else [
                                      {"path": "app.py", "side": "new", "start": 2, "end": 2}]}]})


def test_walkthrough_validation_drops_invented_files_and_lines():
    from acc.server.explanations import validate
    result = validate(walkthrough([
        {"path": "app.py", "side": "old", "start": 2, "end": 2},
        {"path": "../secrets", "side": "new", "start": 1, "end": 1},
        {"path": "app.py", "side": "new", "start": 900, "end": 901},
    ]), DIFF)
    assert result["steps"][0]["references"] == [{"path": "app.py", "side": "old", "start": 2, "end": 2}]


@pytest.mark.parametrize("raw", ["oops", "[]", '{"steps":[]}', '{"steps":[{"title":9}]}', " " * 65537,
                                     walkthrough([{"path": "app.py", "side": "new", "start": True, "end": 2}])])
def test_invalid_walkthrough_is_not_a_success(raw):
    from acc.server.explanations import validate
    with pytest.raises(ValueError):
        validate(raw, DIFF)


def test_large_input_is_reduced_per_file_and_revision_tracks_content():
    from acc.server.explanations import prompt, revision
    large = {**DIFF, "diff": DIFF["diff"] + "+very long content\n" * 40000}
    prepared = prompt(large)
    assert len(prepared.encode()) < 60000
    assert "app.py" in prepared and "summar" in prepared.lower()
    assert "+TWO" in prepared
    assert revision(DIFF) != revision({**DIFF, "diff": DIFF["diff"].replace("TWO", "Two")})


def test_truncated_hunk_cannot_link_to_lines_not_displayed():
    from acc.server.explanations import validate
    changes = {**DIFF, "diff": DIFF["diff"].split("-two")[0], "truncated": True}
    assert validate(walkthrough(), changes)["steps"][0]["references"] == []


def test_header_like_content_is_still_a_displayed_diff_line():
    from acc.server.explanations import validate
    changes = {**DIFF, "diff": DIFF["diff"].replace("-two", "--- a/app.py").replace("+TWO", "+++ b/app.py")}
    refs = [{"path": "app.py", "side": "old", "start": 2, "end": 2},
            {"path": "app.py", "side": "new", "start": 2, "end": 2}]
    assert validate(walkthrough(refs), changes)["steps"][0]["references"] == refs


async def test_explanation_input_redacts_source_keys(api, project, env):
    from test_vault import CANARY, add
    repo(env["workspaces"] / "demo")
    key = await add(api, "Example", "EXAMPLE_KEY")
    await api.post(f"/api/vault/keys/{key['id']}/give", json={"project_id": project["id"]})
    session = await launch(api, project)
    (env["workspaces"] / "demo/app.py").write_text(f"one\n{CANARY}\n")
    response = await api.post(f"/api/sessions/{session['id']}/explanation",
                              json={"provider": "claude", "model": "sonnet"})
    assert response.status_code == 200, response.text
    with api.app.state.db() as db:
        helper = db.get(Session, response.json()["helper_id"])
        assert CANARY not in json.dumps(helper.launch_spec.files)
        assert "key: Example" in helper.launch_spec.files["prompt.txt"]
        assert not helper.vault_keys


@pytest.mark.parametrize("tool,args", [
    ("Edit", {"file_path": "/tmp/example", "content": "x"}),
    ("Bash", {"command": "curl https://example.com"}),
    ("WebFetch", {"url": "https://example.com"}),
    ("Task", {"prompt": "do something"}), ("Read", {"file_path": "/etc/passwd"}),
])
def test_helper_guard_denies_every_tool(tool, args):
    guard = runpy.run_path(str(Path(__file__).parents[1] / "bin/acc-guard"))
    assert guard["check"]({"explanation_only": True}, tool, args, "/tmp") is not None


async def source(api, project, env):
    repo(env["workspaces"] / "demo")
    session = await launch(api, project)
    (env["workspaces"] / "demo" / "app.py").write_text("one\nTWO\n")
    return session


async def explanation(api, sid):
    response = await api.get(f"/api/sessions/{sid}/explanation")
    assert response.status_code == 200, response.text
    return response.json()


async def test_explicit_launch_deduplicates_caches_and_stops(api, project, env):
    session = await source(api, project, env)
    path = f"/api/sessions/{session['id']}/explanation"
    initial = await explanation(api, session["id"])
    assert initial["status"] == "missing"
    replies = await asyncio.gather(*(api.post(path, json={"provider": "claude", "model": "sonnet"})
                                     for _ in range(2)))
    assert all(r.status_code == 200 for r in replies), [r.text for r in replies]
    assert replies[0].json()["helper_id"] == replies[1].json()["helper_id"]

    async def ready():
        value = await explanation(api, session["id"])
        assert value["status"] != "error", value
        return value if value["status"] == "ready" else None
    result = await wait_for(ready)
    helper_id = result["helper_id"]
    assert result["provider"] == "claude" and result["model"] == "sonnet"
    assert result["steps"][0]["references"][0]["path"] == "app.py"
    artifacts = await api.get(f"/api/sessions/{helper_id}/artifacts")
    assert artifacts.status_code == 200, artifacts.text
    assert "walkthrough.json" in {f["name"] for f in artifacts.json()["files"]}
    artifact = await api.get(f"/api/sessions/{helper_id}/artifacts/walkthrough.json")
    assert artifact.status_code == 200 and artifact.json()["steps"]
    with api.app.state.db() as db:
        helper = db.get(Session, helper_id)
        assert helper.owner == "@api" and helper.run_as == env["settings"].default_account
        assert helper.cwd != session.get("cwd")
        assert helper.desired_state == "stopped"
        assert helper.launch_spec.inputs["permission_mode"] == "plan"
        assert helper.launch_spec.inputs["mcps"] == []
        assert json.loads(helper.launch_spec.files["guardrails.json"])["explanation_only"]
        assert len(list(db.scalars(select(Session).where(Session.id != session["id"])))) == 1
    cached = (await api.post(path, json={"provider": "claude", "model": "sonnet"})).json()
    assert cached["helper_id"] == helper_id
    (env["workspaces"] / "demo" / "app.py").write_text("one\nTHREE\n")
    changed = await explanation(api, session["id"])
    assert changed["status"] == "missing" and changed["revision"] != result["revision"]
    regenerated = (await api.post(path, json={"provider": "claude", "model": "sonnet", "regenerate": True})).json()
    assert regenerated["helper_id"] != helper_id


async def test_budget_refuses_before_creating_helper(api, project, env):
    session = await source(api, project, env)
    with write_db(api.app.state.db) as db:
        db.merge(Observation(key="claude.rate_limits", source="test", observed_at=time.time(),
                             value={"five_hour": {"used_percentage": 95, "resets_at": time.time() + 3600}}))
        db.merge(Setting(key="budget.@api", value={"rules": {
            "claude.five_hour": {"mode": "cap", "limit": 50, "action": "pause"}}}))
        db.commit()
    response = await api.post(f"/api/sessions/{session['id']}/explanation",
                              json={"provider": "claude", "model": "sonnet"})
    assert response.status_code == 409 and "budget" in response.text
    with api.app.state.db() as db:
        assert len(list(db.scalars(select(Session)))) == 1


async def test_permissions_and_requester_account(api, project, env, member_accounts):
    from test_auth import cookie_client, do_setup
    from test_users import browser, csrf, join
    session = await source(api, project, env)
    await do_setup(api, env)
    owner = cookie_client(api)
    path = f"/api/sessions/{session['id']}/explanation"
    invite = await owner.post("/api/users/invites", json={"account": "dana-agent"}, headers=csrf(owner))
    async with browser(api) as member:
        assert (await join(member, invite.json()["link"], "alex")).status_code == 200
        assert (await member.get(path)).status_code == 404
        assert (await member.post(path, json={"provider": "claude", "model": "sonnet"},
                                  headers=csrf(member))).status_code == 404
        with write_db(api.app.state.db) as db:
            db.get(Session, session["id"]).owner = "alex"
            db.commit()
        response = await member.post(path, json={"provider": "claude", "model": "sonnet"}, headers=csrf(member))
        assert response.status_code == 200, response.text
        with api.app.state.db() as db:
            helper = db.get(Session, response.json()["helper_id"])
            assert helper.run_as == "dana-agent" and helper.owner == "alex"
        assert (await owner.get(path)).status_code == 200


async def test_regenerate_failed_output_and_no_helper_resume(api, project, env):
    session = await source(api, project, env)
    path = f"/api/sessions/{session['id']}/explanation"
    response = await api.post(path, json={"provider": "claude", "model": "sonnet"})
    helper_id = response.json()["helper_id"]
    await api.app.state.supervisor.stop(helper_id, "@api")
    async def failed():
        value = await explanation(api, session["id"])
        return value if value["status"] == "error" else None
    await wait_for(failed)
    assert (await api.post(f"/api/sessions/{helper_id}/resume")).status_code == 409
    regenerated = await api.post(path, json={"provider": "claude", "model": "sonnet", "regenerate": True})
    assert regenerated.status_code == 200 and regenerated.json()["helper_id"] != helper_id


async def test_signed_out_agent_and_stale_diff_do_not_launch(api, project, env):
    session = await source(api, project, env)
    path = f"/api/sessions/{session['id']}/explanation"
    assert (await api.post(path, json={"provider": "cursor", "model": "test"})).status_code == 422
    assert (await api.post(path, json={"provider": "claude", "model": "sonnet", "revision": "old"})).status_code == 409
    with api.app.state.db() as db:
        assert len(list(db.scalars(select(Session)))) == 1


def test_isolated_cli_home_keeps_auth_but_not_tools_or_instructions(tmp_path):
    from acc.broker.explain import isolated_home
    original, isolated = tmp_path / "original", tmp_path / "isolated"
    (original / ".copilot").mkdir(parents=True)
    (original / ".copilot/config.json").write_text(json.dumps({
        "lastLoggedInUser": {"login": "example"}, "authTokens": {"example": {"token": "example-token"}},
        "mcpServers": {"unsafe": {"command": "unsafe"}}, "customInstructions": "ignore the diff"}))
    isolated_home(original, isolated, "copilot", "/example/bin/acc-hook")
    config = json.loads((isolated / ".copilot/config.json").read_text())
    assert config["authTokens"] == {"example": {"token": "example-token"}}
    assert "mcpServers" not in config and "customInstructions" not in config
    assert (isolated / ".copilot/config.json").stat().st_mode & 0o777 == 0o600


async def test_malformed_agent_output_is_a_visible_error(api, project, env):
    session = await source(api, project, env)
    response = await api.post(f"/api/sessions/{session['id']}/explanation",
                              json={"provider": "claude", "model": "fake-invalid-json"})
    assert response.status_code == 200, response.text
    async def failed():
        value = await explanation(api, session["id"])
        return value if value["status"] == "error" else None
    failure = await wait_for(failed)
    assert "valid walkthrough" in failure["error"]
    assert failure["steps"] == []


@pytest.mark.parametrize("provider", ["claude", "codex", "copilot", "antigravity", "cursor"])
async def test_each_signed_in_native_adapter_completes(api, project, env, provider):
    session = await source(api, project, env)
    if provider == "copilot":
        (env["home"] / ".copilot").mkdir(exist_ok=True)
        (env["home"] / ".copilot/config.json").write_text('{"last_logged_in_user":{"login":"example"}}')
    if provider == "antigravity":
        path = env["home"] / ".gemini/antigravity-cli"
        path.mkdir(parents=True, exist_ok=True)
        (path / "fake-auth").write_text("ok")
    if provider == "cursor":
        (env["home"] / ".cursor").mkdir(exist_ok=True)
        (env["home"] / ".cursor/fake-auth").write_text("ok")
    response = await api.post(f"/api/sessions/{session['id']}/explanation", json={"provider": provider, "model": "test"})
    assert response.status_code == 200, response.text
    async def ready():
        value = await explanation(api, session["id"])
        assert value["status"] != "error", value
        return value if value["status"] == "ready" else None
    assert (await wait_for(ready))["provider"] == provider


def test_stopping_runner_also_stops_the_native_cli(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "original"))
    run_dir = tmp_path / "run"
    (run_dir / "workspace").mkdir(parents=True)
    (run_dir / "prompt.txt").write_text("explain")
    (run_dir / "explanation.json").write_text(json.dumps({"provider": "claude", "model": "test",
                                                        "native_id": "00000000-0000-0000-0000-000000000001"}))
    cli = tmp_path / "cli"
    cli.write_text(f"#!{sys.executable}\nimport os,time\nfrom pathlib import Path\n"
                   "Path('native.pid').write_text(str(os.getpid()))\ntime.sleep(30)\n")
    cli.chmod(0o700)
    root = Path(__file__).resolve().parents[1]
    wrapper = subprocess.Popen([sys.executable, "-m", "acc.broker.explain", str(run_dir), str(cli),
                                str(root / "bin/acc-hook")], env={**os.environ, "PYTHONPATH": str(root / "src")})
    native = None
    try:
        for _ in range(100):
            if (run_dir / "workspace/native.pid").exists():
                native = int((run_dir / "workspace/native.pid").read_text())
                break
            time.sleep(0.02)
        assert native
        wrapper.send_signal(signal.SIGTERM)
        wrapper.wait(timeout=5)
        with pytest.raises(ProcessLookupError):
            os.kill(native, 0)
    finally:
        if wrapper.poll() is None:
            wrapper.kill()
            wrapper.wait()
        if native:
            try:
                os.kill(native, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize("behavior", ["timeout", "oversized"])
def test_runner_enforces_wall_time_and_output_limits(tmp_path, monkeypatch, behavior):
    from acc.broker import explain
    monkeypatch.setenv("HOME", str(tmp_path / "original"))
    (tmp_path / "workspace").mkdir()
    (tmp_path / "prompt.txt").write_text("explain")
    (tmp_path / "explanation.json").write_text(json.dumps({"provider": "claude", "model": "test",
                                                         "native_id": "00000000-0000-0000-0000-000000000001"}))
    cli = tmp_path / "cli"
    cli.write_text(f"#!{sys.executable}\nimport time\n" + (
        "time.sleep(30)\n" if behavior == "timeout" else "print('x' * 300000)\n"))
    cli.chmod(0o700)
    monkeypatch.setattr(explain, "DEADLINE", 0.1)
    assert explain.run(tmp_path, str(cli), "/example/bin/acc-hook") == 1
    status = json.loads((tmp_path / "explanation-status.json").read_text())
    assert ("time limit" if behavior == "timeout" else "output limit") in status["error"]
    assert not (tmp_path / "workspace/.acc/artifacts/walkthrough.json").exists()


def test_runner_never_exposes_partial_completion_status(env, tmp_path, monkeypatch):
    from acc.broker import explain, ops
    acc_id = "0123456789ab"
    run_dir = env["run_dir"] / acc_id
    (run_dir / "workspace").mkdir(parents=True)
    (run_dir / "prompt.txt").write_text("explain")
    (run_dir / "explanation.json").write_text(json.dumps({
        "provider": "claude", "model": "test",
        "native_id": "00000000-0000-0000-0000-000000000001"}))
    cli = tmp_path / "cli"
    cli.write_text(f"#!{sys.executable}\nprint({walkthrough()!r})\n")
    cli.chmod(0o700)
    original_open = Path.open

    def observe_publication(path, mode="r", *args, **kwargs):
        stream = original_open(path, mode, *args, **kwargs)
        if "w" in mode and path.name.startswith("explanation-status"):
            try:
                assert ops.explanation_result(env["broker"], acc_id) == {"status": "running"}
            except BaseException:
                stream.close()
                raise
        return stream

    monkeypatch.setattr(Path, "open", observe_publication)
    assert explain.run(run_dir, str(cli), env["broker"].hook_path) == 0
    result = ops.explanation_result(env["broker"], acc_id)
    assert result["status"] == "done" and json.loads(result["raw"])["steps"]


def test_lent_sign_ins_are_removed_after_the_run(tmp_path):
    """The helper's temporary home must not keep copies of, or links to, the person's CLI credentials."""
    from acc.broker import explain

    run_dir = tmp_path / "run"
    (run_dir / "workspace").mkdir(parents=True)
    (run_dir / "explanation.json").write_text('{"provider": "copilot", "model": "m"}')
    (run_dir / "prompt.txt").write_text("explain")
    home = run_dir / "home"
    (home / ".copilot").mkdir(parents=True)
    (home / ".copilot/config.json").write_text('{"authTokens": "secret"}')
    explain.run(run_dir, "/bin/false", "/bin/true")
    assert not (home / ".copilot/config.json").exists()
