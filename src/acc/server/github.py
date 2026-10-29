"""GitHub for ACC: read views per project (ETag-cached polling), CI failure signals, and repo creation.

Uses the dashboard PAT (data/secrets/github_token, readable only by acc). Agents never see this token;
they use their own gh login in the terminal.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import httpx

API = "https://api.github.com"


class GitHubError(Exception):
    pass


class GitHub:
    def __init__(self, token: str | None, transport: httpx.AsyncBaseTransport | None = None):
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "acc"}
        if token:  # anonymous works for public repos (catalog imports), at a lower rate limit
            headers["Authorization"] = f"Bearer {token}"
        self.client = httpx.AsyncClient(base_url=API, timeout=20, transport=transport, headers=headers)
        self._etags: dict[str, tuple[str, object]] = {}
        self.rate: dict = {}

    async def get(self, path: str, **params):
        """GET with conditional requests: an unchanged resource costs no rate limit (304)."""
        key = path + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        headers = {}
        if key in self._etags:
            headers["If-None-Match"] = self._etags[key][0]
        response = await self.client.get(path, params=params, headers=headers)
        self.rate = {k: response.headers.get(f"x-ratelimit-{k}") for k in ("limit", "remaining", "reset")}
        if response.status_code == 304:
            return self._etags[key][1]
        if response.status_code >= 400:
            raise GitHubError(f"GET {path}: {response.status_code} {response.json().get('message', '') if response.content else ''}")
        data = response.json()
        if etag := response.headers.get("etag"):
            self._etags[key] = (etag, data)
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

    async def snapshot(self, repo: str) -> dict:
        """Everything the project page shows, in one structure."""
        info = await self.get(f"/repos/{repo}")
        default = info["default_branch"]
        branches = await self.get(f"/repos/{repo}/branches", per_page=30)
        commits = await self.get(f"/repos/{repo}/commits", sha=default, per_page=10)
        pulls = await self.get(f"/repos/{repo}/pulls", state="open", per_page=20)
        issues = [i for i in await self.get(f"/repos/{repo}/issues", state="open", per_page=20) if "pull_request" not in i]
        runs = (await self.get(f"/repos/{repo}/actions/runs", per_page=15)).get("workflow_runs", [])
        return {
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
                       "head": p["head"]["ref"], "updated_at": p["updated_at"], "url": p["html_url"]} for p in pulls],
            "issues": [{"number": i["number"], "title": i["title"], "author": i["user"]["login"],
                        "labels": [label["name"] for label in i.get("labels", [])], "updated_at": i["updated_at"],
                        "url": i["html_url"]} for i in issues],
            "runs": [{"id": r["id"], "name": r.get("name"), "branch": r.get("head_branch"), "event": r.get("event"),
                      "status": r.get("status"), "conclusion": r.get("conclusion"), "created_at": r.get("created_at"),
                      "url": r.get("html_url"), "pr": [p["number"] for p in r.get("pull_requests", [])]} for r in runs],
        }

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
    open_heads = {p["head"] for p in snapshot.get("pulls", [])}
    default = snapshot["repo"]["default_branch"]
    latest: dict[tuple, dict] = {}
    for run in snapshot.get("runs", []):  # newest first from the API
        if run["status"] != "completed" or (run["branch"] != default and run["branch"] not in open_heads):
            continue
        latest.setdefault((run["name"], run["branch"]), run)
    return [r for r in latest.values() if r["conclusion"] in ("failure", "timed_out", "startup_failure")]
