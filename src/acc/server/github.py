"""GitHub for ACC: read views per project (ETag-cached polling), CI failure signals, and repo creation.

Uses the dashboard PAT (data/secrets/github_token, readable only by acc). Agents never see this token;
they use their own gh login in the terminal.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from pathlib import Path

import httpx

API = "https://api.github.com"
MAX_ETAGS = 2048
FORCE_REFRESH_COOLDOWN = 15


def same_repo(left, right):
    return bool(isinstance(left, str) and isinstance(right, str) and left and right and left.casefold() == right.casefold())


class GitHubError(Exception):
    pass


class GitHub:
    def __init__(self, token: str | None, transport: httpx.AsyncBaseTransport | None = None):
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "acc"}
        if token:  # anonymous works for public repos (catalog imports), at a lower rate limit
            headers["Authorization"] = f"Bearer {token}"
        self.client = httpx.AsyncClient(base_url=API, timeout=20, transport=transport, headers=headers)
        self._etags: OrderedDict[str, tuple[str, object]] = OrderedDict()
        self.rate: dict = {}
        self._snapshots: dict[str, dict] = {}
        self._snapshot_lock = asyncio.Lock()
        self._forced_refresh_at: dict[str, float] = {}

    async def get(self, path: str, **params):
        """GET with conditional requests: an unchanged resource costs no rate limit (304)."""
        key = path + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        if self.rate.get('remaining') == '0' and float(self.rate.get('reset') or 0) > time.time():
            raise GitHubError('GitHub rate limit reached; waiting for reset')
        headers = {}
        cached = self._etags.get(key)
        if cached:
            headers["If-None-Match"] = cached[0]
            self._etags.move_to_end(key)
        response = await self.client.get(path, params=params, headers=headers)
        self.rate = {k: response.headers.get(f"x-ratelimit-{k}") for k in ("limit", "remaining", "reset")}
        if response.status_code == 304:
            return cached[1]
        if response.status_code >= 400:
            raise GitHubError(f"GET {path}: {response.status_code} {response.json().get('message', '') if response.content else ''}")
        data = response.json()
        if etag := response.headers.get("etag"):
            self._etags[key] = (etag, data)
            self._etags.move_to_end(key)
            while len(self._etags) > MAX_ETAGS:
                self._etags.popitem(last=False)
        return data

    async def post(self, path: str, body: dict) -> dict:
        response = await self.client.post(path, json=body)
        if response.status_code >= 400:
            message = response.json().get("message", "") if response.content else ""
            errors = response.json().get("errors", []) if response.content else []
            raise GitHubError(f"POST {path}: {response.status_code} {message} {json.dumps(errors)[:200] if errors else ''}".strip())
        return response.json()

    async def request(self, method: str, path: str, body: dict | None = None, tail: int = 0) -> tuple[int, object]:
        """Any call, its status and body, for a person's pull request actions (pulls.py decides what a status means).
        `tail`: a text body (a job's log, after GitHub's redirect), only its last `tail` bytes."""
        if tail:
            async with self.client.stream(method, "/" + path, follow_redirects=True) as response:
                kept = bytearray()
                async for chunk in response.aiter_bytes():
                    kept += chunk
                    del kept[:-tail]
                return response.status_code, kept.decode(errors="replace")
        response = await self.client.request(method, "/" + path, json=body)
        try:
            data = response.json() if response.content else None
        except ValueError:
            data = response.text[-2000:]
        return response.status_code, data

    async def snapshot(self, repo: str, heads: tuple[str, ...] = (), *, force: bool = False) -> dict:
        """Everything the project page shows, in one structure."""
        async with self._snapshot_lock:
            repo = repo.casefold()
            if force:
                last = self._forced_refresh_at.get(repo)
                if last is not None and time.monotonic() - last < FORCE_REFRESH_COOLDOWN:
                    if cached := self._snapshots.get(repo):
                        return cached
                    raise GitHubError('GitHub refresh is cooling down; try again in 15 seconds')
                # Failed attempts count too. Shared by every viewer of this repository.
                self._forced_refresh_at[repo] = time.monotonic()
            return await self._snapshot(repo, heads, force=force)

    async def _snapshot(self, repo: str, heads: tuple[str, ...], *, force: bool = False) -> dict:
        repo = repo.casefold()
        cached = self._snapshots.get(repo)
        if not force and cached and time.time() - cached['fetched_at'] < 60:
            return cached
        info = await self.get(f"/repos/{repo}")
        default = info["default_branch"]
        branches = await self.get(f"/repos/{repo}/branches", per_page=30)
        commits = await self.get(f"/repos/{repo}/commits", sha=default, per_page=10)
        pulls = [dict(p) for p in await self.get(f"/repos/{repo}/pulls", state="all", per_page=20)]
        # Fork PRs remain in project views and CI alerts. Only session branch discovery
        # and linking require a matching head repository.
        seen = {p['head']['ref'] for p in pulls
                if same_repo(((p.get('head') or {}).get('repo') or {}).get('full_name'), repo)}
        for branch in heads:
            if branch in seen:
                continue
            linked = await self.get(f'/repos/{repo}/pulls', state='all', head=f"{repo.split('/')[0]}:{branch}", per_page=10)
            linked = [p for p in linked if p.get('head', {}).get('ref') == branch and
                      same_repo(((p.get('head') or {}).get('repo') or {}).get('full_name'), repo)]
            chosen = next((p for p in linked if p.get('state') == 'open'), linked[0] if linked else None)
            if chosen:
                pulls.append(dict(chosen))
                seen.add(branch)
        issues = [i for i in await self.get(f"/repos/{repo}/issues", state="open", per_page=20) if "pull_request" not in i]
        runs = (await self.get(f"/repos/{repo}/actions/runs", per_page=15)).get("workflow_runs", [])
        from .review_state import pull_lifecycle
        for pull in pulls:
            found, reviews = [], []
            if pull.get('state') == 'open' and not pull.get('draft'):
                sha = pull['head']['sha']
                check_runs = (await self.get(f'/repos/{repo}/commits/{sha}/check-runs', per_page=100)).get('check_runs', [])
                latest_checks = {}
                for check in check_runs:  # GitHub returns newest first: a retry replaces the old result
                    latest_checks.setdefault((check.get('name'), (check.get('app') or {}).get('id')), check)
                found = list(latest_checks.values())
                statuses = (await self.get(f'/repos/{repo}/commits/{sha}/status')).get('statuses', [])
                latest_statuses = {}
                for status in statuses:
                    latest_statuses.setdefault(status.get('context'), status)
                found += [{'status': 'in_progress' if c['state'] == 'pending' else 'completed',
                           'conclusion': 'failure' if c['state'] == 'error' else c['state']} for c in latest_statuses.values()]
                reviews = await self.get(f"/repos/{repo}/pulls/{pull['number']}/reviews", per_page=100)
            pull['lifecycle'] = pull_lifecycle(pull, found, reviews)
        snapshot = {
            "fetched_at": time.time(),
            "repo": {"full_name": info["full_name"], "html_url": info["html_url"], "private": info["private"],
                     "default_branch": default, "pushed_at": info.get("pushed_at"), "archived": info.get("archived"),
                     "description": info.get("description")},
            "branches": [{"name": b["name"], "protected": b.get("protected", False), "sha": b["commit"]["sha"][:7]}
                         for b in branches],
            "commits": [{"sha": c["sha"][:7], "message": c["commit"]["message"].split("\n")[0][:120],
                         "author": (c.get("author") or {}).get("login") or c["commit"]["author"]["name"],
                         "date": c["commit"]["author"]["date"], "url": c["html_url"]} for c in commits],
            "pulls": [{"number": p["number"], "title": p["title"], "author": p["user"]["login"], "draft": p.get("draft"),
                       "lifecycle": p["lifecycle"], "state": p.get("state", "open"),
                       "head": p["head"]["ref"], "head_repo": (p["head"].get("repo") or {}).get("full_name"), "updated_at": p["updated_at"], "url": p["html_url"]} for p in pulls],
            "issues": [{"number": i["number"], "title": i["title"], "author": i["user"]["login"],
                        "labels": [label["name"] for label in i.get("labels", [])], "updated_at": i["updated_at"],
                        "url": i["html_url"]} for i in issues],
            "runs": [{"id": r["id"], "name": r.get("name"), "branch": r.get("head_branch"), "event": r.get("event"),
                      "status": r.get("status"), "conclusion": r.get("conclusion"), "created_at": r.get("created_at"),
                      "url": r.get("html_url"), "pr": [p["number"] for p in r.get("pull_requests", [])]} for r in runs],
        }

        self._snapshots[repo] = snapshot
        return snapshot

    async def create_repo(self, name: str, description: str, ruleset_path: Path, auto_init: bool = True,
                          protect: bool = True) -> dict:
        """Private repo; protect-main applied now, or later (protect=False) once a first push has landed."""
        repo = await self.post("/user/repos", {"name": name, "description": description, "private": True,
                                               "auto_init": auto_init, "has_wiki": False, "delete_branch_on_merge": True})
        if protect:
            await self.apply_ruleset(repo["full_name"], ruleset_path)
        return {"full_name": repo["full_name"], "html_url": repo["html_url"], "clone_url": repo["clone_url"]}

    async def apply_ruleset(self, full_name: str, ruleset_path: Path) -> None:
        await self.post(f"/repos/{full_name}/rulesets", json.loads(ruleset_path.read_text()))

    async def close(self) -> None:
        await self.client.aclose()


def ci_failures(snapshot: dict) -> list[dict]:
    """Latest completed run per (workflow, branch) that failed, for the default branch and open PRs."""
    open_heads = {p["head"] for p in snapshot.get("pulls", []) if p.get("state", "open") == "open"}
    default = snapshot["repo"]["default_branch"]
    latest: dict[tuple, dict] = {}
    for run in snapshot.get("runs", []):  # newest first from the API
        if run["status"] != "completed" or (run["branch"] != default and run["branch"] not in open_heads):
            continue
        latest.setdefault((run["name"], run["branch"]), run)
    return [r for r in latest.values() if r["conclusion"] in ("failure", "timed_out", "startup_failure")]
