"""GitHub views, CI failure items, repo creation and import (M8), against a fake GitHub API."""
from __future__ import annotations

import json

import httpx
import pytest

from acc.broker import ops
from acc.server.github import GitHub, ci_failures


class FakeGitHubAPI:
    def __init__(self):
        self.runs = [{"id": 2, "name": "CI", "head_branch": "main", "event": "push", "status": "completed",
                      "conclusion": "failure", "created_at": "2026-10-18T06:00:00Z", "html_url": "https://github.com/o/r/actions/runs/2",
                      "pull_requests": []},
                     {"id": 1, "name": "CI", "head_branch": "main", "event": "push", "status": "completed",
                      "conclusion": "success", "created_at": "2026-10-18T05:00:00Z", "html_url": "u", "pull_requests": []}]
        self.created, self.rulesets, self.calls = [], [], []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path, request.headers.get("if-none-match")))
        if request.method == "GET" and request.headers.get("if-none-match") == '"etag-' + path + '"':
            return httpx.Response(304)
        etag = {"etag": '"etag-' + path + '"'}
        if path == "/repos/o/r":
            return httpx.Response(200, headers=etag, json={"full_name": "o/r", "html_url": "https://github.com/o/r", "private": True,
                                                           "default_branch": "main", "pushed_at": "2026-10-18T06:00:00Z"})
        if path == "/repos/o/r/branches":
            return httpx.Response(200, json=[{"name": "main", "protected": True, "commit": {"sha": "abcdef1234"}}])
        if path == "/repos/o/r/commits":
            return httpx.Response(200, json=[{"sha": "abcdef1234", "html_url": "c", "author": {"login": "casey"},
                                              "commit": {"message": "Fix\n\nbody", "author": {"name": "J", "date": "2026-10-18T06:00:00Z"}}}])
        if path == "/repos/o/r/pulls":
            return httpx.Response(200, json=[])
        if path == "/repos/o/r/issues":
            return httpx.Response(200, json=[{"number": 3, "title": "Bug", "user": {"login": "casey"}, "labels": [],
                                              "updated_at": "x", "html_url": "i"},
                                             {"number": 4, "title": "a PR", "pull_request": {}, "user": {"login": "x"}}])
        if path == "/repos/o/r/actions/runs":
            return httpx.Response(200, json={"workflow_runs": self.runs})
        if path == "/user/repos" and request.method == "POST":
            body = json.loads(request.content)
            self.created.append(body)
            return httpx.Response(201, json={"full_name": f"caseyuafulton/{body['name']}", "html_url": f"https://github.com/caseyuafulton/{body['name']}",
                                             "clone_url": "x"})
        if path.endswith("/rulesets") and request.method == "POST":
            self.rulesets.append((path, json.loads(request.content)))
            return httpx.Response(201, json={"id": 1})
        return httpx.Response(404, json={"message": "Not Found"})


@pytest.fixture
async def gh(api, monkeypatch):
    fake = FakeGitHubAPI()
    api.app.state.supervisor._github = GitHub("t", transport=httpx.MockTransport(fake.handler))
    cloned = []

    def fake_clone(config, repo, dest):  # stands in for `gh repo clone` (no network in tests)
        from pathlib import Path
        Path(dest).mkdir(parents=True)
        cloned.append((repo, dest))
        return {"path": dest}
    monkeypatch.setattr(ops, "clone_repo", fake_clone)
    fake.cloned = cloned
    return fake


async def test_snapshot_uses_etags_and_skips_prs_in_issues(gh):
    client = GitHub("t", transport=httpx.MockTransport(gh.handler))
    first = await client.snapshot("o/r")
    assert [i["number"] for i in first["issues"]] == [3]
    assert first["commits"][0]["message"] == "Fix" and first["branches"][0]["protected"]
    await client.snapshot("o/r")
    assert any(call == ("GET", "/repos/o/r", '"etag-/repos/o/r"') for call in gh.calls)  # conditional request sent


def test_ci_failures_take_the_latest_run_per_workflow_and_branch():
    snapshot = {"repo": {"default_branch": "main"}, "pulls": [{"head": "feature"}], "runs": [
        {"name": "CI", "branch": "main", "status": "completed", "conclusion": "success"},
        {"name": "CI", "branch": "main", "status": "completed", "conclusion": "failure"},  # older: ignored
        {"name": "CI", "branch": "feature", "status": "completed", "conclusion": "failure"},
        {"name": "CI", "branch": "stale-branch", "status": "completed", "conclusion": "failure"},
        {"name": "Lint", "branch": "main", "status": "in_progress", "conclusion": None}]}
    assert [r["branch"] for r in ci_failures(snapshot)] == ["feature"]


async def test_ci_failure_raises_and_then_resolves(api, gh, env):
    project = (await api.post("/api/projects", json={"slug": "r", "name": "R", "repo": "o/r",
                                                     "workspace_path": str(env["workspaces"] / "demo")})).json()
    view = (await api.get(f"/api/projects/{project['id']}/github?refresh=1")).json()
    assert view["repo"]["full_name"] == "o/r" and view["runs"][0]["conclusion"] == "failure"
    items = [i for i in (await api.get("/api/attention")).json() if i["kind"] == "ci_failed"]
    assert items and "CI failed on default branch" in items[0]["title"]
    gh.runs[0]["conclusion"] = "success"
    gh.runs[0]["html_url"] = "changed"  # new data so the fake returns 200, not 304
    api.app.state.supervisor._github._etags.clear()
    await api.get(f"/api/projects/{project['id']}/github?refresh=1")
    assert not [i for i in (await api.get("/api/attention")).json() if i["kind"] == "ci_failed"]


async def test_create_repo_needs_confirmation_then_applies_ruleset_and_registers(api, gh, env):
    refused = await api.post("/api/github/repos", json={"name": "new-thing", "description": "d"})
    assert refused.status_code == 409 and not gh.created
    created = await api.post("/api/github/repos", json={"name": "new-thing", "description": "d", "confirm": True})
    assert created.status_code == 201, created.text
    project = created.json()
    assert gh.created[0]["private"] is True
    assert gh.rulesets[0][0] == "/repos/caseyuafulton/new-thing/rulesets" and gh.rulesets[0][1]["name"] == "protect-main"
    assert project["repo"] == "caseyuafulton/new-thing" and project["trusted"] is True
    assert gh.cloned == [("caseyuafulton/new-thing", str(env["workspaces"] / "new-thing"))]


async def test_import_existing_repo(api, gh, env):
    imported = await api.post("/api/projects/import", json={"repo": "caseyuafulton/some-repo", "trust": False})
    assert imported.status_code == 201, imported.text
    assert imported.json()["slug"] == "some-repo" and imported.json()["trusted"] is False
    again = await api.post("/api/projects/import", json={"repo": "caseyuafulton/some-repo"})
    assert again.status_code == 409


def test_clone_repo_validation(env):
    with pytest.raises(ops.OpError):
        ops.clone_repo(env["broker"], "not a repo", str(env["workspaces"] / "x"))
    with pytest.raises(ops.OpError, match="outside"):
        ops.clone_repo(env["broker"], "o/r", "/tmp/elsewhere")
