"""A session's pull request (docs/PR-FINISHING.md): state, create, update, ready/draft, reviewers, merge, and the
previewed messages that send failed checks or review comments to the session's agent. Owners, and a member on their
own session in their own project; every change is a person's click and is audited."""
from __future__ import annotations

import asyncio
import re
import secrets
import shlex
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text

from .. import claude_auth, guardrails, pulls
from ..broker_client import BrokerError
from ..models import API_PRINCIPAL, AuditLog
from ..supervisor import LaunchRefused
from .context import Context

COMPLETION_SECONDS = 3600
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


class CompletedRebaseIn(BaseModel):
    sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    confirm: bool = False


class BaseUpdateIn(BaseModel):
    mode: str = Field(pattern="^(merge|rebase)$")


class FeedbackIn(BaseModel):
    kind: str = Field(pattern="^(checks|reviews|conflicts)$")


def router(ctx: Context) -> APIRouter:
    api = APIRouter()
    db, supervisor, broker = ctx.db, ctx.supervisor, ctx.broker
    locks = [asyncio.Lock() for _ in range(32)]
    rebases: dict[str, dict] = {}
    pending_rebases: dict[str, dict] = {}  # server-observed, person-started conflicts; never rebase-file authority
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
            return {"repo": project.repo, "branch": branch, "host": session.host or "local", "account": session.run_as,
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

    def person(who, acc_id, action):
        if who.username == API_PRINCIPAL or who.user_id is None:
            audit(who, action, acc_id, {'outcome': 'refused', 'reason': 'person_required'})
            raise HTTPException(403, "A signed-in person must perform this action; agents and orchestrators cannot.")

    def mutable(acc_id, who):
        from acc.backup_activation import held
        with db() as s:
            session = ctx.session_for(s, who, acc_id)
            project = ctx.project_for(s, who, session.project_id)
            if (not project.trusted or project.import_mode == 'readonly' or held(s, session)
                    or not session.worktree or session.container):
                raise HTTPException(403, "Base updates require a trusted, writable session worktree.")
            return guardrails.policy(s, session.cwd, session.owner, project.id,
                                     machine=session.host, account=session.run_as)

    async def unprotected(github, where):
        from urllib.parse import quote
        branch = quote(where['branch'], safe='')
        info = await pulls.call(github, 'GET', f"repos/{where['repo']}")
        code, protection = await github.request('GET', f"repos/{where['repo']}/branches/{branch}")
        rules_code, rules = await github.request('GET', f"repos/{where['repo']}/rules/branches/{branch}")
        if (where['branch'] in {info.get('default_branch'), 'main', 'master'} or code != 200
                or protection.get('protected') or rules_code != 200 or rules):
            raise HTTPException(403, 'Cannot update a default or protected branch; protection must be readable.')
        return info['default_branch']

    @api.get("/api/sessions/{acc_id}/pull/mergeable")
    async def mergeable(acc_id: str, who=Depends(ctx.principal)):
        where = target(acc_id, who)
        github = client(who, where)
        async def inspect():
            pull = await pulls.open_pull(github, where['repo'], where['branch'])
            from urllib.parse import quote
            comparison = await pulls.call(github, 'GET',
                f"repos/{where['repo']}/compare/{quote(pull['base'], safe='')}...{pull['head_sha']}")
            with db() as s:
                session = ctx.session_for(s, who, acc_id)
                automatic = session.provider in ('codex', 'opencode') or (
                    session.provider == 'claude' and session.claude_auth == 'api_key'
                    and claude_auth.mode(s, session) == 'api_key')
            local = await broker.call('session_git', account=where['account'], host=where['host'], path=where['cwd'], action='status')
            with db() as s:
                session = ctx.session_for(s, who, acc_id)
                policy = guardrails.policy(s, session.cwd, session.owner, session.project_id,
                                          machine=session.host, account=session.run_as)
            blocked = policy['rules'].get('force_push', True)
            record = pending_rebases.get(acc_id)
            if (record and record['actor'] == who.username and record['person_id'] == who.user_id
                    and record['branch'] == where['branch'] and record['host'] == where['host']
                    and record['path'] == where['cwd'] and record['expires'] >= time.time()):
                summary_base, summary_before = record['base'], record['before']
            else:
                base = await pulls.call(github, 'GET', f"repos/{where['repo']}/branches/{quote(pull['base'], safe='')}")
                summary_base, summary_before = base['commit']['sha'], pull['head_sha']
            summary = await broker.call('session_rebase_summary', account=where['account'], host=where['host'],
                                       path=where['cwd'], base=summary_base, before=summary_before, head=local['commit'])
            with db() as s:
                summary = supervisor.vault.redactor(s, where['keys']).deep(summary)
            summary['subjects'] = [pulls.display_text(subject, multiline=False) for subject in summary['subjects']]
            return {'summary': summary, 'behind': comparison['behind_by'], 'base': pull['base'], 'automatic': automatic,
                    'mode': 'merge' if blocked else local['base_mode'], 'force_push_blocked': blocked, 'head': local['commit'],
                    'conflicts': [pulls.display_text(p, multiline=False) for p in local['conflicts']]}
        return await run(inspect())

    @api.post("/api/sessions/{acc_id}/pull/mergeable/base")
    async def update_base(acc_id: str, body: BaseUpdateIn, who=Depends(ctx.principal)):
        person(who, acc_id, 'pr.base')
        async with locks[hash(acc_id) % len(locks)]:
            where = target(acc_id, who)
            policy = mutable(acc_id, who)
            github = client(who, where)
            async def update():
                if body.mode == 'rebase' and policy['rules'].get('force_push', True):
                    raise HTTPException(409, 'Block force pushes is on. Refresh and choose Merge base in; no history was rewritten.')
                await unprotected(github, where)
                pull = await pulls.open_pull(github, where['repo'], where['branch'])
                from urllib.parse import quote
                base = await pulls.call(github, 'GET', f"repos/{where['repo']}/branches/{quote(pull['base'], safe='')}")
                return pull, base['commit']['sha'], await broker.call('session_update_base', account=where['account'], host=where['host'],
                    path=where['cwd'], branch=where['branch'], repo=where['repo'], base=pull['base'],
                    sha=base['commit']['sha'], head=pull['head_sha'], mode=body.mode,
                    force_push_blocked=policy['rules'].get('force_push', True),
                    person=who.username, custom=policy['custom'])
            try:
                pull, observed_base, result = await run(update())
            except HTTPException as error:
                audit(who, 'pr.base', acc_id, {'outcome': 'refused', 'status': error.status_code})
                raise
            pending_rebases.pop(acc_id, None)
            if not result['ok'] and result['mode'] == 'rebase' and result['conflicts']:
                # Created only after broker H1 admission and our own GitHub base/head observation.
                for key in list(pending_rebases)[:max(0, len(pending_rebases) - MAX_PREVIEWS + 1)]:
                    pending_rebases.pop(key, None)
                pending_rebases[acc_id] = {'session': acc_id, 'branch': where['branch'], 'actor': who.username,
                    'person_id': who.user_id, 'lease': pull['head_sha'], 'base': observed_base,
                    'before': result['before'], 'expires': time.time() + COMPLETION_SECONDS,
                    'host': where['host'], 'path': where['cwd']}
            with db() as s:
                result = supervisor.vault.redactor(s, where['keys']).deep(result)
            result['output'] = pulls.display_text(result['output'])
            result['conflicts'] = [pulls.display_text(p, multiline=False) for p in result['conflicts']]
            if not result['ok'] and result['mode'] == 'rebase' and result['conflicts']:
                ref = 'refs/heads/' + where['branch']
                command = 'git -c core.hooksPath=/dev/null push ' + shlex.quote('--force-with-lease=' + ref + ':' + pull['head_sha']) + ' origin ' + shlex.quote('HEAD:' + ref)
                result['finish_instructions'] = pulls.display_text(
                    "In this session's own terminal in Rynhult (the session account), not in your own shell, resolve each listed file, stage only the resolved paths with git add -- <path>, "
                    'then run git rebase --continue. Repeat until complete and inspect git log and the final diff. '
                    'Prefer Refresh checklist, then Push completed rebase with Block force pushes OFF. This rechecks the captured remote lease and base ancestry. '
                    'The server remembers this person-started rebase for one hour. If the branch changed on GitHub since it started, review it and start again. '
                    "Fallback only in this session's own terminal in Rynhult as the session account, never your own shell: " + command +
                    '. If the lease refuses, the branch changed on GitHub since the rebase started; review it and start again. Never replace the lease with --force. '
                    'To abandon an unfinished rebase, run git rebase --abort, enable Block force pushes, refresh, and choose Merge base in.')
            audit(who, 'pr.base', acc_id, {k: result[k] for k in ('ok', 'mode', 'before', 'commit', 'conflicts')})
            # The only lease is the remote head observed before this person's successful rebase.
            for key in [k for k, v in rebases.items() if v['expires'] < time.time()] + list(rebases)[:-MAX_PREVIEWS]:
                rebases.pop(key, None)
            if result['ok'] and result['mode'] == 'rebase' and result['before'] != result['commit']:
                ticket = secrets.token_urlsafe(18)
                rebases[ticket] = {'actor': who.username, 'session': acc_id, 'lease': pull['head_sha'],
                                  'head': result['commit'], 'expires': time.time() + PREVIEW_SECONDS,
                                  'branch': where['branch']}
                result['push_ticket'] = ticket
            return result

    @api.post("/api/sessions/{acc_id}/pull/mergeable/completed-rebase")
    async def push_completed_rebase(acc_id: str, body: CompletedRebaseIn, who=Depends(ctx.principal)):
        person(who, acc_id, 'pr.completed_rebase')
        if not body.confirm:
            audit(who, 'pr.completed_rebase', acc_id, {'outcome': 'refused', 'reason': 'consent_required'})
            raise HTTPException(409, 'Confirm pushing the completed rebase with --force-with-lease.')
        async with locks[hash(acc_id) % len(locks)]:
            where = target(acc_id, who)
            policy = mutable(acc_id, who)
            github = client(who, where)
            async def prepare():
                if policy['rules'].get('force_push', True):
                    raise HTTPException(403, 'The Security switch blocks force pushes, including --force-with-lease.')
                await unprotected(github, where)
                pull = await pulls.open_pull(github, where['repo'], where['branch'])
                from urllib.parse import quote
                record = pending_rebases.get(acc_id)
                if record is not None:
                    if (record['actor'] != who.username or record['person_id'] != who.user_id
                            or record['session'] != acc_id or record['branch'] != where['branch']
                            or record['host'] != where['host'] or record['path'] != where['cwd']):
                        raise HTTPException(409, 'This rebase record belongs to another person, session or branch; start again.')
                    if record['expires'] < time.time():
                        raise HTTPException(409, 'The person-started rebase record expired; review the branch and start again.')
                    if pull['head_sha'] != record['lease']:
                        raise HTTPException(409, 'The branch changed on GitHub since the rebase started; review it and start again.')
                    base_sha = record['base']
                else:
                    base = await pulls.call(github, 'GET', f"repos/{where['repo']}/branches/{quote(pull['base'], safe='')}")
                    base_sha = base['commit']['sha']
                checked = await broker.call('session_completed_rebase', account=where['account'], host=where['host'],
                    path=where['cwd'], branch=where['branch'], head=pull['head_sha'], base=base_sha,
                    expected_head=body.sha, validated_lease=record['lease'] if record else None)
                return pull, checked, record
            try:
                pull, checked, record = await run(prepare())
            except HTTPException as error:
                audit(who, 'pr.completed_rebase', acc_id, {'outcome': 'refused', 'status': error.status_code})
                raise
            for key in [k for k, v in rebases.items() if v['expires'] < time.time()] + list(rebases)[:-MAX_PREVIEWS]:
                rebases.pop(key, None)
            lease = record['lease'] if record else pull['head_sha']
            ticket = secrets.token_urlsafe(18)
            rebases[ticket] = {'actor': who.username, 'session': acc_id, 'branch': where['branch'],
                              'head': checked['commit'], 'lease': lease, 'expires': time.time() + PREVIEW_SECONDS}
            audit(who, 'pr.completed_rebase', acc_id, {'outcome': 'prepared', 'head': checked['commit'], 'lease': lease})
        # Reuse the existing policy/protection checks and pinned hardened transport; no nested stripe lock.
        result = await push_rebased(acc_id, ticket, who)
        if result.get('ok') and pending_rebases.get(acc_id) is record:
            pending_rebases.pop(acc_id, None)
        return result

    @api.post("/api/sessions/{acc_id}/pull/mergeable/push/{ticket}")
    async def push_rebased(acc_id: str, ticket: str, who=Depends(ctx.principal)):
        person(who, acc_id, 'pr.push_rebased')
        async with locks[hash(acc_id) % len(locks)]:
            where = target(acc_id, who)
            policy = mutable(acc_id, who)
            entry = rebases.get(ticket)
            if (not entry or entry['actor'] != who.username or entry['session'] != acc_id
                    or entry['branch'] != where['branch'] or entry['expires'] < time.time()):
                audit(who, 'pr.push_rebased', acc_id, {'outcome': 'refused', 'reason': 'invalid_or_expired_ticket'})
                raise HTTPException(409, 'Rebase consent expired; review the branch before pushing.')
            if policy['rules'].get('force_push', True):
                audit(who, 'pr.push_rebased', acc_id, {'outcome': 'refused', 'reason': 'force_push_security_switch'})
                raise HTTPException(403, 'The Security switch blocks force pushes, including --force-with-lease.')
            async def push():
                default = await unprotected(client(who, where), where)
                return await broker.call('session_git', account=where['account'], host=where['host'], path=where['cwd'],
                    action='push', branch=where['branch'], repo=where['repo'], default_branch=default,
                    lease=entry['lease'], expected_head=entry['head'], custom=policy['custom'], person=who.username)
            try:
                result = await run(push())
            except HTTPException as error:
                audit(who, 'pr.push_rebased', acc_id, {'outcome': 'refused', 'status': error.status_code})
                raise
            with db() as s:
                result = supervisor.vault.redactor(s, where['keys']).deep(result)
            result['output'] = pulls.display_text(result['output'])
            audit(who, 'pr.push_rebased', acc_id, {'ok': result['ok'], 'sha': entry['head'], 'lease': entry['lease']})
            if result['ok']:
                rebases.pop(ticket, None)
            return result

    @api.post("/api/sessions/{acc_id}/pull/mergeable/rerun")
    async def rerun_checks(acc_id: str, who=Depends(ctx.principal)):
        person(who, acc_id, 'pr.rerun')
        where = target(acc_id, who)
        github = client(who, where)
        async def rerun():
            pull = await pulls.open_pull(github, where['repo'], where['branch'])
            runs = set()
            for check in (await pulls.checks(github, where['repo'], pull['head_sha']))[:50]:
                if check['kind'] == 'check' and check['app'] == 'github-actions' and check['conclusion'] in pulls.FAILED:
                    job = await pulls.call(github, 'GET', f"repos/{where['repo']}/actions/jobs/{check['id']}")
                    if isinstance(job.get('run_id'), int):
                        runs.add(job['run_id'])
            for number in sorted(runs):
                await pulls.call(github, 'POST', f"repos/{where['repo']}/actions/runs/{number}/rerun-failed-jobs", {})
            return {'runs': sorted(runs)}
        try:
            result = await run(rerun())
        except HTTPException as error:
            audit(who, 'pr.rerun', acc_id, {'outcome': 'refused', 'status': error.status_code})
            raise
        audit(who, 'pr.rerun', acc_id, result)
        return result

    @api.post("/api/sessions/{acc_id}/pull/mergeable/feedback")
    async def guided_feedback(acc_id: str, who=Depends(ctx.principal)):
        person(who, acc_id, 'pr.feedback_preview')
        preview = await pull_feedback(acc_id, FeedbackIn(kind='checks'), who)
        with db() as s:
            session = ctx.session_for(s, who, acc_id)
            automatic = session.provider in ('codex', 'opencode') or (
                session.provider == 'claude' and session.claude_auth == 'api_key'
                and claude_auth.mode(s, session) == 'api_key')
        if preview['id'] and automatic:
            try:
                await pull_send(acc_id, preview['id'], who)
            except HTTPException as error:
                where = target(acc_id, who)
                with db() as s:
                    reason = pulls.display_text(pulls.redact(str(error.detail), supervisor.vault.redactor(s, where['keys'])))[:2000]
                audit(who, 'pr.feedback_preview', acc_id, {'automatic': True, 'items': preview['items'], 'refused': reason})
                return {**preview, 'sent': False, 'refused': reason}
            return {**preview, 'sent': True}
        audit(who, 'pr.feedback_preview', acc_id, {'automatic': False, 'items': preview['items']})
        return {**preview, 'sent': False}

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
        person(who, acc_id, 'pr.merge')
        if not body.confirm:
            audit(who, 'pr.merge', acc_id, {'outcome': 'refused', 'reason': 'consent_required'})
            raise HTTPException(409, "Confirm the merge, then try again.")
        where = target(acc_id, who)
        try:
            merged = await run(pulls.merge(client(who, where), where["repo"], where["branch"], body.method, body.sha))
        except HTTPException as error:
            audit(who, 'pr.merge', acc_id, {'outcome': 'refused', 'status': error.status_code, 'sha': body.sha})
            raise
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
            if body.kind == 'conflicts':
                state = await broker.call('session_git', account=where['account'], host=where['host'], path=where['cwd'], action='status')
                return pull, state['conflicts']
            if body.kind == "checks":
                return pull, await pulls.failed_checks(github, where["repo"], pull["head_sha"])
            return pull, await pulls.review_threads(github, where["repo"], pull["number"])
        pull, items = await run(collect())
        if not items:
            return {"id": None, "text": "", "items": 0, "omitted": 0}
        build = {'checks': pulls.checks_message, 'reviews': pulls.reviews_message, 'conflicts': pulls.conflicts_message}[body.kind]
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
