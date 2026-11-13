"""Supervisor pollers: project autostart, model catalog, GitHub, host snapshot, provider usage, login health.

Mixed into acc.server.supervisor.Supervisor; methods use its state (self.db, self.broker, self.settings, ...).
"""
from __future__ import annotations

import asyncio
import time
import uuid
from contextvars import ContextVar

from sqlalchemy import func, select

from .. import providers
from . import attention, host, vault
from .broker_client import BrokerError, BrokerUnavailable
from .compose import CHANGED, ComposeChanged
from .db import write_in_thread, write_intent
from .github import GitHub, GitHubError, ci_failures, same_repo
from .issue_sources import IssueSourceError, github_remote, source_key
from .models import API_PRINCIPAL, AttentionItem, Event, Observation, Project, PushSubscription, Session, Setting, User
from .push import link_for
from .supervisor_common import log, now

# Task-local so a concurrent test-send request cannot acknowledge an attention claim.
_push_outcome = ContextVar("push_outcome", default=None)


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

    async def refresh_github(self, project_id: int | None = None, *, force: bool = False) -> None:
        """Snapshot each project's repo; raise/resolve CI failure items (default branch and open PRs)."""
        with self.db() as db:
            # Only owners' projects (and ACC's own): snapshots use the owner's GitHub token, so a member's project
            # naming a private repo must not read it through that token (security review 2026-10-22, #1).
            members = select(User.username).where(User.role != "owner")
            query = select(Project).where(Project.archived.is_(False), Project.repo.is_not(None),
                                          (Project.owner.is_(None)) | (Project.owner.not_in(members)))
            if project_id is not None:
                query = query.where(Project.id == project_id)
            projects = []
            for project in db.scalars(query):
                if not github_remote(project.remote_url) or (project.issue_sources or {}).get("gitlab"):
                    continue
                sessions = db.scalars(select(Session).where(Session.project_id == project.id,
                                                           Session.desired_state != 'archived'))
                heads = {(session.changes or {}).get('branch') or session.worktree_branch for session in sessions}
                projects.append((project.id, project.slug, project.repo, tuple(sorted(head for head in heads if head))))
        for pid, slug, repo, heads in projects:
            try:
                kwargs = {'force': True} if force else {}
                snapshot = await self.github.snapshot(repo, heads, **kwargs) if heads else await self.github.snapshot(repo, **kwargs)
            except (GitHubError, OSError) as error:
                log.warning("github %s: %s", repo, error)
                with self.db() as db:
                    previous = db.get(Observation, f"github.{pid}")
                    kept = dict(previous.value or {}) if previous else {}
                    if not same_repo((kept.get('repo') or {}).get('full_name'), repo):
                        kept = {}
                    db.merge(Observation(key=f"github.{pid}", value={**kept, "error": str(error)[:300],
                                                                  "error_at": time.time()},
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
            await self.comment_linear_pulls(pid, [p for p in snapshot.get("pulls", []) if p.get("state", "open") == "open"])

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
                     username: str | None = None, on_result=None) -> int:
        """Send one message to subscribed devices (or one endpoint); drop devices the push service says are gone.
        everyone=False: owners' devices only; username: that person's devices only."""
        if self.push is None:
            return 0
        if on_result is None:
            on_result = _push_outcome.get()
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
            if on_result is not None:
                on_result(status)
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
        """Durable recipient queue with summary releases and bounded leased retries."""
        if self.push is None:
            return
        async with self._push_lock:
            await self._push_attention_locked()

    async def _push_attention_locked(self) -> None:
        messages = []
        stamp = time.time()
        with self.db() as db:
            state = db.get(Setting, "push.delivery")
            newest = db.scalar(select(func.max(AttentionItem.id))) or 0
            if state is None:
                db.add(Setting(key="push.delivery", value={"cursor": newest, "pending": [], "inflight": []}))
                db.commit()
                self._push_cursor = newest
                return
            cursor = state.value["cursor"]
            # Upgrade the original pair-only queue conservatively as previously held.
            pending = [entry if isinstance(entry, dict) else
                       {"item": entry[0], "person": entry[1], "seen": True, "attempts": 0}
                       for entry in state.value["pending"]]
            inflight = []
            for claim in state.value.get("inflight", []):
                if stamp - claim["claimed_at"] < 120:
                    inflight.append(claim)
                    continue
                exhausted = [entry for entry in claim["items"] if entry["attempts"] >= 3]
                if exhausted:
                    log.warning("push delivery exhausted after 3 attempts: %s items recipient %s",
                                len(exhausted), claim["person"])
                pending.extend(entry for entry in claim["items"] if entry["attempts"] < 3)
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
            from .api.inbox import permission_info
            from .approval_cards import push_body

            def notification_body(item):
                facts = permission_info(db, item.session_id).get("approval_card") if item.kind == "needs_input" else None
                if item.kind == "needs_input":
                    body = push_body(facts or {"command": None, "label": "No specific hint"}, item.detail or item.source)
                    session = db.get(Session, item.session_id) if facts else None
                    if session and session.parent_session_id:
                        body += " · Parent chain: " + " → ".join([*session.parent_chain, session.id])
                    return body
                return item.detail or item.source

            def security_notice(item):
                return item.kind in {"sign_in", "backup"} or attention.persistent_reason(item) is not None

            recipients = set(db.scalars(select(PushSubscription.username)))
            owners = set(db.scalars(select(User.username).where(User.role == "owner"))) | {API_PRINCIPAL}
            for item in db.scalars(select(AttentionItem).where(AttentionItem.id > cursor, AttentionItem.id <= newest,
                    AttentionItem.severity.in_(("warn", "critical")), AttentionItem.resolved_at.is_(None),
                    (AttentionItem.snoozed_until.is_(None)) | (AttentionItem.snoozed_until <= now()))
                    .order_by(AttentionItem.id)):
                # None denotes the existing owners-only broadcast, not a person's subscription.
                audience = [None] if not item.owner and security_notice(item) else sorted(
                    recipients & ({item.owner} if item.owner else owners))
                for person in audience:
                    pending.append({"item": item.id, "person": person, "seen": False, "attempts": 0})
            remaining, sendable = [], {}
            inflight = [claim for claim in inflight if claim["person"] in recipients or
                        (claim["person"] is None and recipients & owners)]
            for entry in pending:
                item_id, person = entry["item"], entry["person"]
                item = db.get(AttentionItem, item_id)
                if item is None or item.resolved_at or item.acked_at or not current_group_recipient(item):
                    continue
                if person is None:
                    if item.owner or not security_notice(item):
                        continue
                elif person not in recipients or person not in ({item.owner} if item.owner else owners):
                    continue
                if item.snoozed_until and item.snoozed_until > now():
                    remaining.append(entry)
                    continue
                preference = db.get(Setting, f"push.active.{person}") if person is not None else None
                always = bool(preference and preference.value.get("always"))
                exempt = item.severity == "critical" or security_notice(item)
                if not exempt and not always and self.push_presence.watching(person, item.session_id):
                    remaining.append({**entry, "seen": True})
                    continue
                sendable.setdefault(person, []).append((entry, item))

            def claim_message(person, entries, message):
                claimed = [{**entry, "attempts": entry["attempts"] + 1} for entry in entries]
                claim = {"id": uuid.uuid4().hex, "person": person, "claimed_at": stamp,
                         "attempts": max(entry["attempts"] for entry in claimed), "items": claimed}
                inflight.append(claim)
                messages.append((message, person, claim["id"]))

            def individual(person, entry, item):
                claim_message(person, [entry], {"title": item.title, "body": notification_body(item),
                              "url": f"/inbox?item={item.id}" if item.kind == "needs_input" and item.session_id else link_for(item), "tag": item.dedupe_key, "critical": item.severity == "critical"})

            def summary(person, entries, critical=False):
                count = len(entries)
                noun = "item still needs" if count == 1 else "items still need"
                title = f"{count} {'critical ' if critical else ''}{noun} you"
                claim_message(person, entries, {"title": title, "url": "/inbox",
                              "tag": "critical-summary" if critical else "presence-summary", "critical": critical})

            for person, ready in sendable.items():
                ordinary, critical_overflow = [], []
                critical_sent = 0
                for entry, item in ready:
                    if security_notice(item):
                        individual(person, entry, item)
                    elif item.severity == "critical":
                        if critical_sent < 5 and not entry.get("critical_summary"):
                            individual(person, entry, item)
                            critical_sent += 1
                        else:
                            critical_overflow.append({**entry, "critical_summary": True, "seen": True})
                    else:
                        ordinary.append((entry, item))
                if critical_overflow:
                    summary(person, critical_overflow, critical=True)
                if len(ordinary) > 3 or any(entry["seen"] for entry, _ in ordinary):
                    summary(person, [{**entry, "seen": True} for entry, _ in ordinary])
                else:
                    for entry, item in ordinary:
                        individual(person, entry, item)
            value = {"cursor": newest, "pending": remaining, "inflight": inflight}
            if state.value != value:
                state.value = value
                db.commit()
            self._push_cursor = newest
        for message, person, claim_id in messages:
            accepted, retryable = False, False

            def outcome(status):
                nonlocal accepted, retryable
                retryable |= status == 0 or status == 429 or status >= 500
                if 200 <= status < 300 and not accepted:
                    self._finish_push_claim(claim_id)
                    accepted = True

            token = _push_outcome.set(outcome)
            try:
                if person is None:
                    await self.notify(message, everyone=False)
                else:
                    await self.notify(message, username=person)
            finally:
                _push_outcome.reset(token)
            if not accepted:
                with self.db() as db:
                    audience = (PushSubscription.username.in_(owners) if person is None else
                                PushSubscription.username == person)
                    devices = db.scalar(select(PushSubscription.id).where(audience).limit(1))
                if not devices or not retryable:
                    self._finish_push_claim(claim_id)

    def _finish_push_claim(self, claim_id):
        """Persist first-device acceptance before another device's network request."""
        with self.db() as db:
            state = db.get(Setting, "push.delivery")
            state.value = {**state.value, "inflight": [claim for claim in state.value.get("inflight", [])
                                                     if claim["id"] != claim_id]}
            db.commit()
