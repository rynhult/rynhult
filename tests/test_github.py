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
    client._snapshots["o/r"]["fetched_at"] -= 61
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
    api.app.state.supervisor._github._snapshots.clear()
    api.app.state.supervisor._github._forced_refresh_at.clear()
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


async def test_refresh_failure_keeps_the_last_snapshot(api, gh, env, monkeypatch):
    from acc.server.github import GitHubError

    project = (await api.post('/api/projects', json={'slug': 'cached', 'name': 'Cached', 'repo': 'o/r',
        'workspace_path': str(env['workspaces'] / 'demo')})).json()
    path = f"/api/projects/{project['id']}/github?refresh=1"
    previous = (await api.get(path)).json()
    assert previous['repo']['full_name'] == 'o/r'

    async def unavailable(*args, **kwargs):
        raise GitHubError('GitHub rate limit reached; waiting for reset')

    monkeypatch.setattr(api.app.state.supervisor._github, 'snapshot', unavailable)
    cached = (await api.get(path)).json()
    assert cached['repo'] == previous['repo']
    assert cached['fetched_at'] == previous['fetched_at']
    assert 'rate limit' in cached['error']


async def test_explicit_refresh_bypasses_snapshot_cache_once_but_not_rate_limit(monkeypatch):
    import asyncio
    import time
    from types import SimpleNamespace

    from acc.server import github as module

    tick = [1000.0]
    monkeypatch.setattr(module, 'time', SimpleNamespace(time=time.time, monotonic=lambda: tick[0]))

    from acc.server.github import GitHubError

    fake = FakeGitHubAPI()
    github = GitHub('fixture', transport=httpx.MockTransport(fake.handler))
    try:
        await github.snapshot('O/R')
        count = len(fake.calls)
        await github.snapshot('o/r')
        assert len(fake.calls) == count
        await github.snapshot('O/R', force=True)
        assert len(fake.calls) > count
        count = len(fake.calls)
        await github.snapshot('o/r')
        assert len(fake.calls) == count
        # Simultaneous viewers, including differently cased repo names, share the cooldown.
        await asyncio.gather(*(github.snapshot(repo, force=True) for repo in ('o/r', 'O/R', 'o/r')))
        assert len(fake.calls) == count
        tick[0] += 14.999
        await github.snapshot('o/r', force=True)
        assert len(fake.calls) == count
        tick[0] = 1015.0
        await github.snapshot('o/r', force=True)
        assert len(fake.calls) > count
        count = len(fake.calls)
        tick[0] += 15
        github.rate = {'remaining': '0', 'reset': str(time.time() + 60)}
        with pytest.raises(GitHubError, match='rate limit'):
            await github.snapshot('o/r', force=True)
        assert len(fake.calls) == count
    finally:
        await github.close()


async def test_etag_lru_is_bounded_and_keeps_recent_304_bodies(monkeypatch):
    from acc.server import github as module

    monkeypatch.setattr(module, 'MAX_ETAGS', 2)
    calls = []

    def handler(request):
        calls.append((request.url.path, request.headers.get('if-none-match')))
        if request.headers.get('if-none-match'):
            return httpx.Response(304)
        return httpx.Response(200, headers={'etag': request.url.path}, json={'path': request.url.path})

    github = GitHub('fixture', transport=httpx.MockTransport(handler))
    try:
        await github.get('/a')
        await github.get('/b')
        assert await github.get('/a') == {'path': '/a'}
        await github.get('/c')
        assert len(github._etags) == 2
        assert '/b?' not in github._etags
        await github.get('/b')
        assert calls[-1] == ('/b', None)
        assert len(github._etags) == 2
    finally:
        await github.close()


async def test_repo_edit_never_returns_previous_repo_data_after_refresh_failure(api, gh, env, monkeypatch):
    from helpers import write_db

    from acc.server.github import GitHubError
    from acc.server.models import Project

    project = (await api.post('/api/projects', json={'slug': 'edited', 'name': 'Edited', 'repo': 'o/r',
        'workspace_path': str(env['workspaces'] / 'demo')})).json()
    path = f"/api/projects/{project['id']}/github"
    assert (await api.get(path + '?refresh=1')).json()['repo']['full_name'] == 'o/r'
    with write_db(api.app.state.db) as db:
        db.get(Project, project['id']).repo = 'O/R'
        db.commit()
    assert (await api.get(path)).json()['repo']['full_name'] == 'o/r'
    with write_db(api.app.state.db) as db:
        db.get(Project, project['id']).repo = 'other/project'
        db.commit()
    assert 'repo' not in (await api.get(path)).json()

    async def unavailable(*args, **kwargs):
        raise GitHubError('unavailable')

    monkeypatch.setattr(api.app.state.supervisor._github, 'snapshot', unavailable)
    value = (await api.get(path + '?refresh=1')).json()
    assert value['error'] == 'unavailable'
    assert not {'repo', 'pulls', 'branches', 'commits'} & value.keys()


async def test_failed_explicit_refresh_also_has_a_shared_repo_cooldown(monkeypatch):
    import time
    from types import SimpleNamespace

    from acc.server import github as module
    from acc.server.github import GitHubError

    tick = [1000.0]
    monkeypatch.setattr(module, 'time', SimpleNamespace(time=time.time, monotonic=lambda: tick[0]))
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(503, json={'message': 'unavailable'})

    github = GitHub('fixture', transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(GitHubError, match='503'):
            await github.snapshot('example/one', force=True)
        with pytest.raises(GitHubError, match='cooling down'):
            await github.snapshot('EXAMPLE/ONE', force=True)
        assert len(calls) == 1
        with pytest.raises(GitHubError, match='503'):
            await github.snapshot('example/two', force=True)
        assert len(calls) == 2  # another repository has its own timer
        tick[0] += 15
        with pytest.raises(GitHubError, match='503'):
            await github.snapshot('example/one', force=True)
        assert len(calls) == 3
    finally:
        await github.close()


async def test_twelve_project_five_pr_poll_reuses_all_response_cache_entries(monkeypatch):
    import time
    from types import SimpleNamespace

    from acc.server import github as module

    stamp = [time.time()]
    monkeypatch.setattr(module, 'time', SimpleNamespace(time=lambda: stamp[0], monotonic=time.monotonic))
    calls = []
    not_modified = []

    def handler(request):
        calls.append(request)
        etag = '"' + str(request.url) + '"'
        if request.headers.get('if-none-match') == etag:
            not_modified.append(request)
            return httpx.Response(304)
        parts = request.url.path.strip('/').split('/')
        repo, tail = '/'.join(parts[1:3]), '/'.join(parts[3:])
        if not tail:
            data = {'full_name': repo, 'html_url': f'https://github.com/{repo}', 'private': True, 'default_branch': 'main'}
        elif tail == 'pulls' and not request.url.params.get('head'):
            data = [{'number': n + 1, 'title': f'Change {n}', 'user': {'login': 'person'}, 'state': 'open',
                     'head': {'ref': f'feature-{n}', 'sha': f'sha-{n}', 'repo': {'full_name': repo}},
                     'updated_at': 'today', 'html_url': f'https://github.com/{repo}/pull/{n + 1}'} for n in range(5)]
        elif tail == 'actions/runs':
            data = {'workflow_runs': []}
        elif tail.endswith('/check-runs'):
            data = {'check_runs': []}
        elif tail.endswith('/status'):
            data = {'statuses': []}
        else:
            data = []
        return httpx.Response(200, json=data, headers={'etag': etag})

    github = GitHub('fixture', transport=httpx.MockTransport(handler))
    try:
        heads = tuple(f'feature-{n}' for n in range(5)) + ('without-pr-a', 'without-pr-b')
        for n in range(12):
            snapshot = await github.snapshot(f'example/project-{n}', heads)
            assert len(snapshot['pulls']) == 5
        first_count = len(calls)
        assert first_count == 276  # 21 base/PR reads + 2 missing-session-head lookups per repo
        assert len(github._etags) == first_count
        stamp[0] += 180  # next scheduled poll; no explicit-refresh bypass
        for n in range(12):
            await github.snapshot(f'example/project-{n}', heads)
        assert len(calls) == first_count * 2
        assert len(not_modified) == first_count
        assert all(request.headers.get('if-none-match') for request in calls[first_count:])
        assert len(github._etags) == first_count
    finally:
        await github.close()


async def test_fork_pr_remains_in_project_snapshot_and_ci_failures():
    fake = FakeGitHubAPI()
    fake.runs[0]['head_branch'] = 'contributor-fix'

    def handler(request):
        if request.url.path == '/repos/o/r/pulls':
            return httpx.Response(200, json=[{'number': 99, 'title': 'Fork contribution', 'state': 'open',
                'user': {'login': 'contributor'}, 'head': {'ref': 'contributor-fix', 'sha': 'fork-sha',
                    'repo': {'full_name': 'contributor/r'}}, 'updated_at': 'today',
                'html_url': 'https://github.com/o/r/pull/99'}])
        if request.url.path.endswith('/check-runs'):
            return httpx.Response(200, json={'check_runs': []})
        if request.url.path.endswith('/status'):
            return httpx.Response(200, json={'statuses': []})
        if request.url.path.endswith('/reviews'):
            return httpx.Response(200, json=[])
        return fake.handler(request)

    github = GitHub('fixture', transport=httpx.MockTransport(handler))
    try:
        snapshot = await github.snapshot('o/r')
        assert snapshot['pulls'][0]['head_repo'] == 'contributor/r'
        assert snapshot['pulls'][0]['number'] == 99
        assert any(run['branch'] == 'contributor-fix' for run in ci_failures(snapshot))
    finally:
        await github.close()
