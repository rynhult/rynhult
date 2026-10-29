"""Supervisor pollers: project autostart, model catalog, GitHub, host snapshot, provider usage, login health.

Mixed into acc.server.supervisor.Supervisor; methods use its state (self.db, self.broker, self.settings, ...).
"""
from __future__ import annotations

import asyncio
import time

from sqlalchemy import func, select

from .. import providers
from . import attention, host, vault
from .broker_client import BrokerError, BrokerUnavailable
from .compose import CHANGED, ComposeChanged
from .db import write_in_thread, write_intent
from .github import GitHub, GitHubError, ci_failures
from .issue_sources import IssueSourceError, github_remote, source_key
from .models import API_PRINCIPAL, AttentionItem, Event, Observation, Project, PushSubscription, Session, User
from .push import link_for
from .supervisor_common import log, now


class PollingMixin:
    async def autostart_projects(self) -> bool:
        """Bring flagged compose stacks up once per accd start; True when every one succeeded or was held.

        A stack runs only while its compose files are what an owner approved (D4, acc.server.compose): otherwise it is
        held for this start, with an Inbox item and the change on its project card, until an owner approves."""
        with self.db() as db:
            projects = [(p.id, p.slug, p.workspace_path, p.host or "local", (p.compose_pin or {}).get("digest")) for p in db.scalars(
                select(Project).where(Project.compose_autostart.is_(True), Project.archived.is_(False)))]
        all_ok = True
        for project_id, slug, path, machine, approved in projects:
            if project_id in self._autostarted:
                continue
            async with self.workspace_operations.hold(path, machine, wait=False) as held:
                if not held:
                    all_ok = False
                    continue
                with self.db() as db:
                    project = db.get(Project, project_id)
                    if project is None or project.archived or not project.compose_autostart:
                        continue
                try:
                    if approved is None:
                        raise ComposeChanged("its compose files were never approved")
                    await self.broker.call("compose_up", account=self.settings.services_owner, host=machine, path=path, expect=approved)
                    self._autostarted.add(project_id)
                    with self.db() as db:
                        db.add(Event(source="supervisor", kind="project_autostart", payload={"project": slug}))
                        db.commit()
                except BrokerUnavailable:
                    raise
                except (BrokerError, ComposeChanged) as error:
                    if isinstance(error, ComposeChanged) or CHANGED in str(error):
                        self._autostarted.add(project_id)  # held until an owner approves (which brings it up)
                        self.hold_autostart(project_id, slug, str(error))
                        continue
                    all_ok = False  # e.g. the user's Docker is not up yet after boot: retry
                    log.warning("autostart %s: %s", slug, error)
        return all_ok

    def release_autostart(self, project_id: int) -> bool:
        """An owner approved a held stack: True when autostart held it this start (so the caller brings it up)."""
        if project_id in self._autostart_held:
            self._autostart_held.discard(project_id)
            return True
        return False

    def hold_autostart(self, project_id: int, slug: str, reason: str) -> None:
        self._autostart_held.add(project_id)
        log.warning("autostart %s held: %s", slug, reason)
        with self.db() as db:
            db.add(Event(source="supervisor", kind="project_autostart_held", payload={"project": slug, "reason": reason}))
            attention.raise_item(db, f"compose:{project_id}", kind="compose", severity="warn", source="autostart",
                                 owner="", title=f"{slug}: autostart held until an owner approves its compose files",
                                 detail="The stack runs as the projects' owner, and an agent can edit its compose "
                                        "files. Open the project and use Approve compose files (in its Docker stack "
                                        f"card) to read them and bring the stack up. ({reason})")
            db.commit()

    async def refresh_models(self) -> dict:
        catalog = await self.broker.call("model_catalog")

        def work():
            with self.db() as db:
                db.merge(Observation(key="models", value=catalog, source="broker", observed_at=catalog["fetched_at"]))
                db.commit()

        await write_in_thread(work)
        return catalog

    def _models_stale(self) -> bool:
        if getattr(self.broker, 'authority', None):
            return False  # node-specific model requests query their execution broker
        with self.db() as db:
            current = db.get(Observation, "models")
            return current is None or time.time() - current.observed_at > self.settings.models_interval

    @property
    def github(self) -> GitHub:
        if self._github is None:
            self._github = GitHub(self.settings.github_token_path.read_text().strip())
        return self._github

    def github_available(self) -> bool:
        """ACC has the dashboard's GitHub token (owners' GitHub views and pull request actions)."""
        return self._github is not None or self.settings.github_token_path.exists()

    async def refresh_github(self, project_id: int | None = None) -> None:
        """Snapshot each project's repo; raise/resolve CI failure items (default branch and open PRs)."""
        with self.db() as db:
            # Only owners' projects (and ACC's own): snapshots use the owner's GitHub token, so a member's project
            # naming a private repo must not read it through that token (security review 2026-10-22, #1).
            members = select(User.username).where(User.role != "owner")
            query = select(Project).where(Project.archived.is_(False), Project.repo.is_not(None),
                                          (Project.owner.is_(None)) | (Project.owner.not_in(members)))
            if project_id is not None:
                query = query.where(Project.id == project_id)
            projects = [(p.id, p.slug, p.repo) for p in db.scalars(query)
                        if github_remote(p.remote_url) and not (p.issue_sources or {}).get("gitlab")]
        for pid, slug, repo in projects:
            try:
                snapshot = await self.github.snapshot(repo)
            except (GitHubError, OSError) as error:
                log.warning("github %s: %s", repo, error)
                with self.db() as db:
                    db.merge(Observation(key=f"github.{pid}", value={"error": str(error)[:300], "fetched_at": time.time()},
                                         source="github", observed_at=time.time()))
                    db.commit()
                continue
            failures = {f"ci:{pid}:{r['name']}:{r['branch']}": r for r in ci_failures(snapshot)}
            with self.db() as db:
                db.merge(Observation(key=f"github.{pid}", value=snapshot, source="github", observed_at=snapshot["fetched_at"]))
                for key, run in failures.items():
                    where = "default branch" if run["branch"] == snapshot["repo"]["default_branch"] else f"PR branch {run['branch']}"
                    attention.raise_item(db, key, kind="ci_failed", severity="warn", source="github actions",
                                         title=f"{slug}: {run['name']} failed on {where}",
                                         detail=f"{run['url']} ({run['event']}, {run['created_at']})")
                open_keys = db.scalars(select(AttentionItem.dedupe_key).where(
                    AttentionItem.dedupe_key.like(f"ci:{pid}:%"), AttentionItem.resolved_at.is_(None))).all()
                for key in open_keys:
                    if key not in failures:
                        attention.resolve(db, key)
                db.commit()
            await self.comment_linear_pulls(pid, snapshot.get("pulls", []))

    async def refresh_issue_comments(self) -> None:
        from .admin_features import enabled
        if not enabled(self.settings, 'issue_sources'):
            return
        with self.db() as db:
            pending = [(session.id, session.project_id, session.worktree_branch)
                       for session in db.scalars(select(Session).where(Session.issue_source.is_not(None),
                                                                       Session.worktree_branch.is_not(None)))
                       if (session.issue_source or {}).get("source") == "linear"
                       and not session.issue_source.get("comment_attempted")
                       and (session.project.issue_sources or {}).get("gitlab")
                       and (session.project.issue_sources or {}).get("linear", {}).get("comment_pr")
                       and not session.project.archived]
        for sid, pid, branch in pending:
            try:
                with self.db() as db:
                    session = db.get(Session, sid)
                    actor = session.issue_source.get("actor")
                    user = db.scalar(select(User).where(User.username == actor))
                    if actor != API_PRINCIPAL and not (user and (user.role == "owner" or session.project.owner == actor)):
                        raise IssueSourceError("the session's launcher no longer has access to this project")
                    config, token = source_key(db, session.project, "gitlab", actor, self.vault)
                    remote = session.project.remote_url
                view = await self.issue_sources.list("gitlab", config, remote, token, kind="merge_request", branch=branch)
                await self.comment_linear_pulls(pid, [{"head": i["branch"], "url": i["url"]} for i in view["items"]])
            except (IssueSourceError, vault.VaultError) as error:
                with self.db() as db:
                    session = db.get(Session, sid)
                    session.issue_source = {**session.issue_source, "comment_status": "blocked", "comment_error": str(error)}
                    db.commit()

    async def comment_linear_pulls(self, project_id: int, pulls: list[dict]) -> None:
        """Only opted-in Linear sessions on their own branch; an ambiguous send is never retried automatically."""
        from .admin_features import enabled
        if not enabled(self.settings, 'issue_sources'):
            return
        with self.db() as db:
            ids = list(db.scalars(select(Session.id).where(Session.project_id == project_id,
                                                           Session.issue_source.is_not(None))))
        for sid in ids:
            with self.db() as db:
                session = db.get(Session, sid)
                project = session.project
                ref = session.issue_source or {}
                config = (project.issue_sources or {}).get("linear", {})
                if (ref.get("source") != "linear" or not config.get("comment_pr") or project.archived
                        or ref.get("comment_attempted") or not session.worktree_branch):
                    continue
                pull = next((p for p in pulls if p.get("head") == session.worktree_branch), None)
                if pull is None:
                    continue
                actor = ref.get("actor")
                user = db.scalar(select(User).where(User.username == actor))
                permitted = actor == API_PRINCIPAL or user and (user.role == "owner" or project.owner == actor)
                try:
                    if not permitted:
                        raise IssueSourceError("the session's launcher no longer has access to this project")
                    config, token = source_key(db, project, "linear", actor, self.vault)
                except (IssueSourceError, vault.VaultError) as error:
                    session.issue_source = {**ref, "comment_status": "blocked", "comment_error": str(error)}
                    db.commit()
                    continue
                remote, item_id = project.remote_url, ref["id"]
            try:
                await self.issue_sources.detail("linear", config, remote, token, "issue", item_id)
            except IssueSourceError as error:
                with self.db() as db:
                    session = db.get(Session, sid)
                    session.issue_source = {**session.issue_source, "comment_status": "blocked", "comment_error": str(error)}
                    db.commit()
                continue
            with self.db() as db:
                session = db.get(Session, sid)
                if session.issue_source.get("comment_attempted"):
                    continue
                current = (session.project.issue_sources or {}).get("linear", {})
                if not current.get("comment_pr") or current != config:
                    continue
                try:
                    user = db.scalar(select(User).where(User.username == actor))
                    if session.project.archived or (actor != API_PRINCIPAL and not (
                            user and (user.role == "owner" or session.project.owner == actor))):
                        raise IssueSourceError("the session's launcher no longer has access to this project")
                    _, token = source_key(db, session.project, "linear", actor, self.vault)
                except (IssueSourceError, vault.VaultError) as error:
                    session.issue_source = {**session.issue_source, "comment_status": "blocked", "comment_error": str(error)}
                    db.commit()
                    continue
                session.issue_source = {**session.issue_source, "comment_attempted": True, "comment_status": "sending",
                                        "comment_error": None}
                db.commit()
            try:
                await self.issue_sources.comment(token, item_id, pull["url"])
                status, error = "sent", None
            except IssueSourceError as failure:
                status, error = "uncertain", str(failure) + "; not retried automatically to avoid duplicate comments"
            with self.db() as db:
                session = db.get(Session, sid)
                session.issue_source = {**session.issue_source, "comment_status": status, "comment_error": error}
                db.add(Event(session_id=sid, source="supervisor", kind="linear_comment",
                             payload={"status": status, "error": error}))
                db.commit()

    def host_view(self) -> dict:
        """Live readings without attention writes; safe for concurrent GET requests."""
        return {"snapshot": host.read_snapshot(self.settings.hoststate_path), "live": host.live()}

    @write_intent
    def refresh_host(self) -> dict:
        snapshot = host.read_snapshot(self.settings.hoststate_path)
        current = host.live()
        with self.db() as db:
            host.evaluate(db, snapshot, current)
            db.commit()
        return {"snapshot": snapshot, "live": current}

    async def refresh_providers(self) -> dict:
        """Each installed provider's plan usage from its CLI (Claude: the CLI cache, a labelled fallback to the statusline)."""
        account = self.broker.default_account
        results = {}
        try:
            installed = await self.broker.call("installed_providers", account=account)
        except BrokerError:
            installed = {}
        for provider in providers.PROVIDERS.values():
            if "usage" not in provider.supports or not installed.get(provider.id):
                continue
            key = provider.usage_observation
            try:
                value = await self.broker.call("usage", provider=provider.id, account=account)
            except BrokerError as error:
                value = {"error": str(error)[:200]}
            if value is None:
                continue
            if isinstance(value, dict):
                value = {**value, "_scope": {"account": account, "machine": "local"}}
            results[key] = value
            with self.db() as db:
                db.merge(Observation(key=key, value=value, source=f"{provider.id} usage", observed_at=time.time()))
                db.commit()
        return results

    async def refresh_auth(self, account: str | None = None, host: str | None = None) -> dict:
        """Each agent account's own Claude and Codex logins (or one account's). Returns the asked-for (or default)
        account's health; `auth.health` keeps the default account's for older readers. With `host`, that account's
        on another machine (cached as auth.health.host.<machine>.<account>); a full pass also asks every connected
        machine's brokers."""
        if host and host != "local":
            return await self._remote_auth(host, account or self.broker.default_account)
        if account is None and self.broker.hub is not None:
            for name, link in list(self.broker.hub.links.items()):
                for remote in [] if link.closed else link.accounts:
                    try:
                        await asyncio.wait_for(self._remote_auth(name, remote), 60)
                    except (BrokerError, ConnectionError, asyncio.TimeoutError) as error:
                        log.info("logins on %s for %s: %s", name, remote, error)
        wanted = [account] if account else self.broker.accounts()
        result = {}
        for name in wanted:
            try:
                health = await self.broker.call("auth_health", account=name)
            except BrokerError:
                if account or name == self.broker.default_account:
                    raise
                continue
            default = name == self.broker.default_account
            with self.db() as db:
                db.merge(Observation(key=f"auth.health.{name}", value=health, source="broker", observed_at=health["checked_at"]))
                if default:
                    db.merge(Observation(key="auth.health", value=health, source="broker", observed_at=health["checked_at"]))
                attention.evaluate_auth(db, health, name, key_suffix="" if default else f":{name}")
                db.commit()
            if default or account:
                result = health
        return result

    async def _remote_auth(self, host: str, account: str) -> dict:
        health = await self.broker.call("auth_health", account=account, host=host)
        with self.db() as db:
            db.merge(Observation(key=f"auth.health.host.{host}.{account}", value=health, source=f"broker:{host}",
                                 observed_at=health["checked_at"]))
            db.commit()
        return health

    # ---------------------------------------------------------------- push notifications

    async def notify(self, payload: dict, only: str | None = None, everyone: bool = True,
                     username: str | None = None) -> int:
        """Send one message to subscribed devices (or one endpoint); drop devices the push service says are gone.
        everyone=False: owners' devices only; username: that person's devices only."""
        if self.push is None:
            return 0
        with self.db() as db:
            query = select(PushSubscription)
            if only:
                query = query.where(PushSubscription.endpoint == only)
            if username is not None:  # one person's devices only (their test message)
                query = query.where(PushSubscription.username == username)
            if not everyone:
                owners = select(User.username).where(User.role == "owner")
                query = query.where(PushSubscription.username.in_(owners) | (PushSubscription.username == API_PRINCIPAL))  # the owner's token
            devices = [(d.id, d.endpoint, d.p256dh, d.auth) for d in db.scalars(query)]
        delivered = 0
        for device_id, endpoint, p256dh, auth in devices:
            from .admin_features import enabled
            if not enabled(self.settings, 'notifications'):
                break
            try:
                status = await self.push.send(endpoint, p256dh, auth, payload)
            except Exception as error:  # network or encryption trouble: keep the device, count the failure
                log.warning("push to device %s failed: %s", device_id, error)
                status = 0
            with self.db() as db:
                device = db.get(PushSubscription, device_id)
                if device is None:
                    continue
                if status in (404, 410):  # unsubscribed or expired at the push service
                    db.delete(device)
                elif 200 <= status < 300:
                    device.last_ok_at, device.failures = now(), 0
                    delivered += 1
                else:
                    device.failures += 1
                db.commit()
        return delivered

    async def push_attention(self) -> None:
        """New inbox items that matter become notifications (the cursor starts at the newest item, so a restart
        never replays old ones)."""
        if self.push is None:
            return
        with self.db() as db:
            newest = db.scalar(select(func.max(AttentionItem.id))) or 0
            if self._push_cursor is None:
                self._push_cursor = newest
                return
            items = list(db.scalars(select(AttentionItem).where(
                AttentionItem.id > self._push_cursor, AttentionItem.severity.in_(("warn", "critical")),
                AttentionItem.resolved_at.is_(None),
                (AttentionItem.snoozed_until.is_(None)) | (AttentionItem.snoozed_until <= now())).order_by(AttentionItem.id)))
            self._push_cursor = newest
            def current_group_recipient(item):
                if item.source != 'group_board':
                    return True
                from . import group_board
                from .models import Group, GroupMembership, User
                if item.session_id:
                    session = db.get(Session, item.session_id)
                    return bool(session and group_board.authorized(db, session, self.settings.groups_remote_enabled) and session.owner == item.owner)
                parts = item.dedupe_key.split(':')
                group = db.get(Group, parts[1]) if len(parts) > 2 and parts[0] == 'group-ops' else None
                user = db.scalar(select(User).where(User.username == item.owner))
                member = db.get(GroupMembership, (group.id, user.id)) if group and user else None
                return bool(group and not group.archived and member and member.role == 'maintainer')
            messages = [({"title": i.title, "body": i.detail or i.source, "url": link_for(i), "tag": i.dedupe_key,
                          "critical": i.severity == "critical"}, i.owner) for i in items if current_group_recipient(i)]
        for message, owner in messages[:10]:  # a burst (e.g. a reboot) shouldn't become a flood
            if owner:  # someone's own item (their session, their account): only their devices
                await self.notify(message, username=owner)
            else:      # host-wide and approvals: owners' devices
                await self.notify(message, everyone=False)
