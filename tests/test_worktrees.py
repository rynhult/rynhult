"""Sessions in their own git worktree and branch."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from helpers import wait_for
from isolated_settings import isolated_broker
from test_changes import git, repo
from test_supervisor import state_is

from acc.broker import ops


def branches(path):
    return subprocess.run(["git", "-C", str(path), "branch", "--format=%(refname:short)"], capture_output=True,
                          text=True).stdout.split()


def test_make_and_remove(tmp_path):
    config = isolated_broker(tmp_path, socket_path=tmp_path / "s", run_dir=tmp_path, tmux_socket="x", allowed_roots=(tmp_path,),
                          allowed_uids=frozenset(), providers={}, hook_path="x")
    repo(tmp_path / "proj")
    made = ops.make_worktree(config, str(tmp_path / "proj"), str(tmp_path), "proj-abc12345", "acc/fix-abc12345")
    tree = Path(made["path"])
    assert tree == tmp_path / ".worktrees" / "proj-abc12345" and (tree / "app.py").exists()
    assert "acc/fix-abc12345" in branches(tmp_path / "proj")
    (tree / "app.py").write_text("changed\n")  # uncommitted: kept
    kept = ops.remove_worktree(config, str(tmp_path / "proj"), str(tree))
    assert kept["removed"] is False and "uncommitted" in kept["reason"] and tree.exists()
    git(tree, "commit", "-qam", "work")  # committed: the folder goes, the branch and its commit stay
    assert ops.remove_worktree(config, str(tmp_path / "proj"), str(tree)) == {"removed": True}
    assert not tree.exists() and "acc/fix-abc12345" in branches(tmp_path / "proj")
    with pytest.raises(ops.OpError):
        ops.make_worktree(config, str(tmp_path / "proj"), str(tmp_path), "x", "main")  # only acc/ branches
    (tmp_path / "plain").mkdir()
    with pytest.raises(ops.OpError, match="isn't a git repository"):
        ops.make_worktree(config, str(tmp_path / "plain"), str(tmp_path), "plain-1", "acc/x-1")
    with pytest.raises(ops.OpError):
        ops.remove_worktree(config, str(tmp_path / "proj"), str(tmp_path / "proj"))  # not under .worktrees


async def test_session_in_its_own_worktree(api, project, env):
    repo(env["workspaces"] / "demo")
    body = {"project_id": project["id"], "provider": "claude", "name": "Fix Login", "worktree": True}
    session = (await api.post("/api/sessions", json=body)).json()
    assert session["worktree"].startswith(str(env["workspaces"] / ".worktrees" / "demo-"))
    assert session["worktree_branch"].startswith("acc/fix-login-") and session["cwd"] == session["worktree"]
    assert json.loads(session["launch_spec"]["files"]["guardrails.json"])["workspace"] == session["worktree"]
    await wait_for(state_is(api, session["id"], "idle"))
    assert session["worktree_branch"] in branches(env["workspaces"] / "demo")

    (Path(session["worktree"]) / "app.py").write_text("half done\n")
    await api.post(f"/api/sessions/{session['id']}/archive")
    items = [i for i in (await api.get("/api/attention")).json() if i["kind"] == "worktree_kept"]
    assert items and "uncommitted" in items[0]["title"] and Path(session["worktree"]).exists()

    clean = (await api.post("/api/sessions", json={**body, "name": "second"})).json()
    await wait_for(state_is(api, clean["id"], "idle"))
    await api.post(f"/api/sessions/{clean['id']}/archive")
    assert not Path(clean["worktree"]).exists()
    events = (await api.get(f"/api/sessions/{clean['id']}/events")).json()
    assert any(e["kind"] == "worktree_removed" for e in events)


async def test_worktree_needs_a_git_project(api, project, env):
    response = await api.post("/api/sessions", json={"project_id": project["id"], "provider": "claude", "name": "x",
                                                     "worktree": True})
    assert response.status_code == 409 and "Make the first commit" in response.json()["detail"]
