"""A small, stateful fake of the GitHub API pull requests use (tests and the e2e server): one repository, its
branches and rules, pull requests, checks, Actions job logs, review threads, and the GraphQL mutations ACC calls."""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, unquote

import httpx

SHA = "a" * 40


class FakeGitHub:
    def __init__(self, repo: str = "example-org/demo"):
        self.repo = repo
        self.info = {"full_name": repo, "html_url": f"https://github.com/{repo}", "private": True, "default_branch": "main",
                     "allow_merge_commit": False, "allow_squash_merge": True, "allow_rebase_merge": True}
        self.branches = {"main": SHA.replace("a", "0"), "feature/login": SHA}
        self.protection = {"main": {"required_status_checks": {"enforcement_level": "everyone", "contexts": ["test"]}}}
        self.rules = {"main": [{"type": "pull_request", "parameters": {"required_approving_review_count": 0,
                                                                       "allowed_merge_methods": ["squash", "merge"]}}]}
        self.pulls: dict[int, dict] = {}
        self.check_runs: dict[str, list[dict]] = {}
        self.statuses: dict[str, list[dict]] = {}
        self.jobs: dict[int, dict] = {}
        self.logs: dict[int, str] = {}
        self.threads: dict[int, list[dict]] = {}
        self.calls: list[tuple[str, str, object]] = []
        self.merged: list[dict] = []
        self.refuse_merge: tuple[int, str] | None = None

    # ---- set-up helpers
    def open_pull(self, branch: str = "feature/login", draft: bool = False, state: str = "clean") -> dict:
        number = len(self.pulls) + 1
        self.pulls[number] = {"number": number, "title": "Add login", "body": "", "state": "open", "draft": draft,
                              "merged": False, "head": {"ref": branch, "sha": self.branches[branch]},
                              "base": {"ref": "main"}, "node_id": f"PR_{number}", "mergeable": True,
                              "mergeable_state": state, "requested_reviewers": [], "user": {"login": "demo-agent"},
                              "html_url": f"https://github.com/{self.repo}/pull/{number}"}
        return self.pulls[number]

    def check(self, name: str, conclusion: str | None, job: int | None = None, sha: str = SHA, app: str = "github-actions",
              output: dict | None = None):
        self.check_runs.setdefault(sha, []).append({
            "id": job or (900 + len(self.check_runs.get(sha, []))), "name": name, "app": {"slug": app},
            "status": "completed" if conclusion else "in_progress", "conclusion": conclusion,
            "html_url": f"https://github.com/{self.repo}/actions/runs/1/job/{job}", "output": output or {}})

    # ---- the API
    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = unquote(request.url.path), request.method
        body = json.loads(request.content) if request.content else None
        self.calls.append((method, path, body))
        return self.respond(method, path, parse_qs(request.url.query.decode()), body)

    def respond(self, method: str, path: str, query: dict, body) -> httpx.Response:
        base = f"/repos/{self.repo}"
        if path == "/graphql":
            return self.graphql(body)
        if not path.startswith(base):
            return httpx.Response(404, json={"message": "Not Found"})
        rest = path[len(base):]
        if rest == "" and method == "GET":
            return httpx.Response(200, json=self.info)
        if m := re.fullmatch(r"/branches/(.+)", rest):
            name = m.group(1)
            if name not in self.branches:
                return httpx.Response(404, json={"message": "Branch not found"})
            protection = self.protection.get(name)
            return httpx.Response(200, json={"name": name, "commit": {"sha": self.branches[name]}, "protected": bool(protection),
                                             **({"protection": protection} if protection else {})})
        if m := re.fullmatch(r"/rules/branches/(.+)", rest):
            return httpx.Response(200, json=self.rules.get(m.group(1), []))
        if rest == "/pulls" and method == "GET":
            head = (query.get("head") or [""])[0].split(":", 1)[-1]
            found = [p for p in reversed(self.pulls.values()) if not head or p["head"]["ref"] == head]
            return httpx.Response(200, json=found)
        if rest == "/pulls" and method == "POST":
            if body["head"] not in self.branches:
                return httpx.Response(422, json={"message": "Validation Failed", "errors": [{"field": "head", "code": "invalid"}]})
            pull = self.open_pull(body["head"], draft=body.get("draft", False))
            pull.update(title=body["title"], body=body.get("body", ""))
            pull["base"]["ref"] = body["base"]
            return httpx.Response(201, json=pull)
        if m := re.fullmatch(r"/pulls/(\d+)", rest):
            pull = self.pulls.get(int(m.group(1)))
            if pull is None:
                return httpx.Response(404, json={"message": "Not Found"})
            if method == "PATCH":
                pull.update({k: v for k, v in body.items() if k in ("title", "body")})
            return httpx.Response(200, json=pull)
        if m := re.fullmatch(r"/pulls/(\d+)/requested_reviewers", rest):
            pull = self.pulls[int(m.group(1))]
            if "ghost-user" in body["reviewers"]:
                return httpx.Response(422, json={"message": "Reviews may only be requested from collaborators."})
            pull["requested_reviewers"] += [{"login": r} for r in body["reviewers"]]
            return httpx.Response(201, json=pull)
        if m := re.fullmatch(r"/pulls/(\d+)/merge", rest):
            pull = self.pulls[int(m.group(1))]
            if self.refuse_merge:
                return httpx.Response(self.refuse_merge[0], json={"message": self.refuse_merge[1]})
            if body.get("sha") != pull["head"]["sha"]:
                return httpx.Response(409, json={"message": "Head branch was modified. Review and try the merge again."})
            pull.update(state="closed", merged=True)
            self.merged.append({"number": pull["number"], **body})
            return httpx.Response(200, json={"merged": True, "sha": "b" * 40, "message": "Pull Request successfully merged"})
        if m := re.fullmatch(r"/commits/([0-9a-f]+)/check-runs", rest):
            runs = self.check_runs.get(m.group(1), [])
            return httpx.Response(200, json={"total_count": len(runs), "check_runs": runs})
        if m := re.fullmatch(r"/commits/([0-9a-f]+)/status", rest):
            return httpx.Response(200, json={"state": "success", "statuses": self.statuses.get(m.group(1), [])})
        if m := re.fullmatch(r"/actions/jobs/(\d+)", rest):
            job = self.jobs.get(int(m.group(1)))
            return httpx.Response(200, json=job) if job else httpx.Response(404, json={"message": "Not Found"})
        if m := re.fullmatch(r"/actions/jobs/(\d+)/logs", rest):
            log = self.logs.get(int(m.group(1)))
            return httpx.Response(200, text=log) if log is not None else httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(404, json={"message": "Not Found"})

    def graphql(self, body: dict) -> httpx.Response:
        query, variables = body["query"], body.get("variables") or {}
        if "reviewThreads" in query:
            threads = self.threads.get(variables["number"], [])
            return httpx.Response(200, json={"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": threads}}}}})
        number = int(variables["id"].split("_")[1])
        if number not in self.pulls:
            return httpx.Response(200, json={"errors": [{"message": "Could not resolve to a node"}]})
        self.pulls[number]["draft"] = "convertPullRequestToDraft" in query
        if self.pulls[number]["draft"]:
            self.pulls[number]["mergeable_state"] = "draft"
        elif self.pulls[number]["mergeable_state"] == "draft":
            self.pulls[number]["mergeable_state"] = "clean"
        return httpx.Response(200, json={"data": {"x": {"pullRequest": {"isDraft": self.pulls[number]["draft"]}}}})

    def thread(self, number: int, path: str, line: int, *comments: tuple[str, str], resolved: bool = False,
               outdated: bool = False) -> None:
        self.threads.setdefault(number, []).append({
            "isResolved": resolved, "isOutdated": outdated, "path": path, "line": line, "originalLine": line,
            "comments": {"nodes": [{"author": {"login": a}, "body": b} for a, b in comments]}})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)
