"""A session's pull request (docs/PR-FINISHING.md): state, create, update, ready/draft, reviewers, merge, and the
previewed messages that send failed checks or review comments to the session's agent. Owners, and a member on their
own session in their own project; every change is a person's click and is audited."""
from __future__ import annotations

import re
import secrets
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text

from .. import pulls
from ..broker_client import BrokerError
from ..models import AuditLog
from ..supervisor import LaunchRefused
from .context import Context

PREVIEW_SECONDS = 600
MAX_PREVIEWS = 200  # at most ~4 KB each; the oldest go first
MAX_BODY = 60000
LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")


class PullIn(BaseModel):
    title: str = Field(min_length=1, max_length=256)
    body: str = Field("", max_length=MAX_BODY)
    base: str | None = Field(None, max_length=200, pattern=r"^[^\s~^:?*\[\\]+$")
    draft: bool = False


class PullUpdateIn(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=256)
    body: str | None = Field(None, max_length=MAX_BODY)


class ReadyIn(BaseModel):
    ready: bool


class ReviewersIn(BaseModel):
    reviewers: list[str] = Field(min_length=1, max_length=15)

    @field_validator("reviewers")
    @classmethod
    def _logins(cls, value: list[str]) -> list[str]:
        logins = [v.strip().lstrip("@") for v in value if v.strip()]
        if not logins or any(not LOGIN.match(v) for v in logins):
            raise ValueError("enter reviewers by their GitHub usernames (letters, digits and hyphens)")
        return logins


class MergeIn(BaseModel):
    method: str = Field(pattern="^(merge|squash|rebase)$")
    sha: str = Field(pattern=r"^[0-9a-f]{40}$")  # the head the person saw: a newer push makes GitHub refuse
    confirm: bool = False


class FeedbackIn(BaseModel):
    kind: str = Field(pattern="^(checks|reviews)$")


def router(ctx: Context) -> APIRouter:
    api = APIRouter()
    db, supervisor, broker = ctx.db, ctx.supervisor, ctx.broker
    previews: dict[str, dict] = {}  # preview id -> the exact text the person saw, for them and that session only

    def target(acc_id: str, who) -> dict:
        with db() as s:
            session = ctx.session_for(s, who, acc_id)
            project = session.project
            if not who.is_owner and project.owner != who.username:
                raise HTTPException(404, "That session wasn't found.")
            if session.workdir or project.private_workspace:
                raise HTTPException(409, "Pull requests are for project sessions. This workspace has no repository.")
            if not project.repo:
                raise HTTPException(409, "This project has no GitHub repository, so it has no pull requests.")
            branch = (session.changes or {}).get("branch") or session.worktree_branch
            if not branch:
                raise HTTPException(409, "The session's folder isn't on a branch. Ask the agent to create one, then try again.")
            return {"repo": project.repo, "branch": branch, "host": project.host or "local", "account": session.run_as,
                    "cwd": session.cwd, "name": session.name, "keys": session.vault_keys}

    def client(who, where: dict):
        """The person's own GitHub sign-in: the dashboard token for owners (as the GitHub views), otherwise their
        agent account's gh login through its broker. A member's project never goes through the owners' token."""
        if who.is_owner and supervisor.github_available():
            return supervisor.github
        return pulls.BrokerGitHub(broker, who.account, where["host"])

    async def run(coroutine):
        try:
            return await coroutine
        except pulls.PullError as error:
            raise HTTPException(error.status, str(error)) from None
        except BrokerError as error:
            raise ctx.refused(error) from None
        except (httpx.HTTPError, OSError) as error:
            raise HTTPException(502, f"GitHub: {type(error).__name__}") from None
        except (KeyError, TypeError, AttributeError, ValueError):  # an answer in a shape Rynhult doesn't read: never a 500
            raise HTTPException(502, "GitHub sent an answer Rynhult couldn't read. Try again in a minute.") from None

    def audit(who, action: str, acc_id: str, detail: dict) -> None:
        with db() as s:
            s.add(AuditLog(actor=who.username, action=action, target=acc_id, detail=detail))
            s.commit()

    @api.get("/api/sessions/{acc_id}/pull")
    async def pull_status(acc_id: str, who=Depends(ctx.principal)):
        where = target(acc_id, who)
        github = client(who, where)
        out = await run(pulls.status(github, where["repo"], where["branch"]))
        out["signed_in"] = "token" if github is supervisor.github else "gh"
        return out

    @api.get("/api/sessions/{acc_id}/pull/draft")
    async def pull_draft(acc_id: str, who=Depends(ctx.principal)):
        """A title and body to start from: the session's handoff note, else its last summary (redacted)."""
        where = target(acc_id, who)
        title, body, source = where["name"], "", "session name"
        try:
            found = await broker.call("scan_docs", account=where["account"], host=where["host"], path=where["cwd"])
            mine = sorted((e for e in found.get("handoffs", []) if acc_id in e["path"]), key=lambda e: e["path"])
            if mine:
                doc = await broker.call("read_doc", account=where["account"], host=where["host"], path=where["cwd"],
                                        rel=mine[-1]["path"])
                body, source = doc["content"], f"handoff {mine[-1]['path']}"
        except BrokerError:
            pass
        if not body:
            with db() as s:
                last = s.execute(text("SELECT text FROM transcript_fts WHERE session_id = :id AND role = 'assistant' "
                                      "ORDER BY seq DESC LIMIT 1"), {"id": acc_id}).scalar()
            if last:
                body, source = last, "last summary"
        heading = re.search(r"^#\s+(.+)$", body, re.M)
        if heading:
            title = re.sub(r"^(handoff|hand-off)\s*[:—-]\s*", "", heading.group(1).strip(), flags=re.I) or title
        with db() as s:
            redactor = supervisor.vault.redactor(s, where["keys"])
            body = pulls.redact(body.strip(), redactor)[:MAX_BODY - 200]
            title = pulls.redact(title, redactor)[:256]
        if body:
            body += f"\n\n---\nPrepared in Rynhult from session {where['name']} ({source})."
        return {"title": title, "body": body, "source": source}

    @api.post("/api/sessions/{acc_id}/pull", status_code=201)
    async def pull_create(acc_id: str, body: PullIn, who=Depends(ctx.principal)):
        where = target(acc_id, who)
        created = await run(pulls.create(client(who, where), where["repo"], where["branch"], body.title.strip(),
                                         body.body, body.base, body.draft))
        audit(who, "pr.create", acc_id, {"repo": where["repo"], "branch": where["branch"], "number": created["number"],
                                         "title": created["title"], "draft": body.draft})
        return created

    @api.patch("/api/sessions/{acc_id}/pull")
    async def pull_update(acc_id: str, body: PullUpdateIn, who=Depends(ctx.principal)):
        fields = body.model_dump(exclude_none=True)
        if not fields:
            raise HTTPException(422, "There's nothing to change. Edit the title or description first.")
        where = target(acc_id, who)
        github = client(who, where)

        async def change():
            pull = await pulls.open_pull(github, where["repo"], where["branch"])
            return await pulls.update(github, where["repo"], pull["number"], fields)
        updated = await run(change())
        audit(who, "pr.update", acc_id, {"repo": where["repo"], "number": updated["number"], "fields": sorted(fields),
                                         **({"title": fields["title"]} if "title" in fields else {})})
        return updated

    @api.post("/api/sessions/{acc_id}/pull/ready")
    async def pull_ready(acc_id: str, body: ReadyIn, who=Depends(ctx.principal)):
        where = target(acc_id, who)
        github = client(who, where)

        async def change():
            pull = await pulls.open_pull(github, where["repo"], where["branch"])
            await pulls.set_ready(github, pull, body.ready)
            return pull
        pull = await run(change())
        audit(who, "pr.ready", acc_id, {"repo": where["repo"], "number": pull["number"], "ready": body.ready})
        return {"number": pull["number"], "draft": not body.ready}

    @api.post("/api/sessions/{acc_id}/pull/reviewers")
    async def pull_reviewers(acc_id: str, body: ReviewersIn, who=Depends(ctx.principal)):
        where = target(acc_id, who)
        github = client(who, where)

        async def change():
            pull = await pulls.open_pull(github, where["repo"], where["branch"])
            return await pulls.request_reviewers(github, where["repo"], pull["number"], body.reviewers)
        updated = await run(change())
        audit(who, "pr.update", acc_id, {"repo": where["repo"], "number": updated["number"], "reviewers": body.reviewers})
        return updated

    @api.post("/api/sessions/{acc_id}/pull/merge")
    async def pull_merge(acc_id: str, body: MergeIn, who=Depends(ctx.principal)):
        """A person's merge, confirmed; refused while anything blocks it. Agents have no route to this."""
        if not body.confirm:
            raise HTTPException(409, "Confirm the merge, then try again.")
        where = target(acc_id, who)
        merged = await run(pulls.merge(client(who, where), where["repo"], where["branch"], body.method, body.sha))
        audit(who, "pr.merge", acc_id, {"repo": where["repo"], "number": merged["number"], "method": body.method,
                                        "sha": body.sha, "merged": merged["merged"]})
        return merged

    @api.post("/api/sessions/{acc_id}/pull/feedback")
    async def pull_feedback(acc_id: str, body: FeedbackIn, who=Depends(ctx.principal)):
        """Build the message (redacted, trimmed, framed as untrusted) and keep it for sending exactly as shown."""
        where = target(acc_id, who)
        github = client(who, where)

        async def collect():
            pull = await pulls.find(github, where["repo"], where["branch"])
            if pull is None:
                raise pulls.PullError(f"{where['branch']} has no pull request", 404)
            if body.kind == "checks":
                return pull, await pulls.failed_checks(github, where["repo"], pull["head_sha"])
            return pull, await pulls.review_threads(github, where["repo"], pull["number"])
        pull, items = await run(collect())
        if not items:
            return {"id": None, "text": "", "items": 0, "omitted": 0}
        build = pulls.checks_message if body.kind == "checks" else pulls.reviews_message
        with db() as s:
            message, omitted = build(items, pull, where["repo"], where["branch"], who.username,
                                     supervisor.vault.redactor(s, where["keys"]))
        now = time.time()
        for key in [k for k, v in previews.items() if v["expires"] < now] + list(previews)[:-MAX_PREVIEWS]:
            previews.pop(key, None)
        preview = secrets.token_urlsafe(12)
        previews[preview] = {"actor": who.username, "session": acc_id, "text": message, "kind": body.kind,
                             "number": pull["number"], "items": len(items) - omitted, "expires": now + PREVIEW_SECONDS}
        return {"id": preview, "text": message, "items": len(items) - omitted, "omitted": omitted}

    @api.post("/api/sessions/{acc_id}/pull/feedback/{preview}/send")
    async def pull_send(acc_id: str, preview: str, who=Depends(ctx.principal)):
        target(acc_id, who)
        entry = previews.get(preview)
        if entry is None or entry["actor"] != who.username or entry["session"] != acc_id or entry["expires"] < time.time():
            raise HTTPException(404, "That preview has expired. Open it again before sending.")
        try:
            await supervisor.send_text(acc_id, who.username, entry["text"])
        except (LaunchRefused, BrokerError) as error:
            raise ctx.refused(error) from None
        previews.pop(preview, None)  # sent once
        audit(who, "pr.send_to_agent", acc_id, {"kind": entry["kind"], "number": entry["number"],
                                                "items": entry["items"], "chars": len(entry["text"])})
        return {"sent": True, "chars": len(entry["text"])}

    return api
