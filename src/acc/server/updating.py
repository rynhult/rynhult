"""Owner updates: daily release checks, deduplicated Inbox, scheduling and helper reconciliation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from datetime import datetime
from functools import cached_property

from sqlalchemy import select

from .. import __version__, bundle
from ..update_helper import read_json, write_json
from ..updates import BUILD, GIT_MESSAGE, REPO, SOURCE_ERRORS, ReleaseSourceError, available, check, repository
from .ha import fence
from .models import AttentionItem, AuditLog, Node, Observation, Setting

log = logging.getLogger("acc.server")


CONFIG_ERROR = "Updater configuration is corrupt or unreadable. Repair updates-config.json or save update settings before updating."


NODE_RESPONSE_TIMEOUT = 120
NODE_INSTALLER_MESSAGE = "This machine didn't respond to the update. Its Rynhult may be too old to update itself. Use the recovery steps below."


def node_update_state(node):
    state = (node.info or {}).get("update") or {}
    if state.get("awaiting_result") and time.time() - state.get("requested_at", 0) >= NODE_RESPONSE_TIMEOUT:
        state = {**state, "running": False, "awaiting_result": False, "ok": False,
                 "installer_required": True, "detail": NODE_INSTALLER_MESSAGE}
        node.info = {**(node.info or {}), "update": state}
    if state.get("installer_required"):
        state = {**state, "detail": NODE_INSTALLER_MESSAGE}
    return state or None


class ConfigError(ValueError):
    pass


class Updates:
    def __init__(self, ctx):
        self.ctx, self.db, self.settings = ctx, ctx.db, ctx.settings
        self.lock = asyncio.Lock()
        self.last_follow = {}

    @cached_property
    def build(self):
        return bundle.installed_identity(self.settings.update_source)

    def authorized_repository(self):
        return (
            self.settings.update_repository
            if self.settings.update_repository is not None
            else repository(self.settings.source_url)
        )

    def defaults(self):
        return {"repository": self.authorized_repository(), "automatic": False, "time": "03:00", "follow": {}}

    def config(self):
        try:
            saved = read_json(self.settings.data_dir / "updates-config.json")
        except FileNotFoundError:
            return self.defaults()
        except (OSError, ValueError):
            log.warning(CONFIG_ERROR)
            raise ConfigError(CONFIG_ERROR) from None
        value = {**self.defaults(), **saved}
        repo, at, follow = value["repository"], value["time"], value["follow"]
        if (
            set(saved) - set(self.defaults())
            or not isinstance(repo, str) or len(repo) > 200
            or (repo and (not REPO.fullmatch(repo) or any(p in (".", "..") for p in repo.split("/"))))
            or type(value["automatic"]) is not bool
            or not isinstance(at, str) or not re.fullmatch(r"([01][0-9]|2[0-3]):[0-5][0-9]", at)
            or not isinstance(follow, dict)
            or any(not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", k) or type(v) is not bool for k, v in follow.items())
        ):
            log.warning(CONFIG_ERROR)
            raise ConfigError(CONFIG_ERROR)
        return value

    def observation(self):
        with self.db() as s:
            item = s.get(Observation, "updates.release")
            # Preserve an earlier install's fetched state until the next successful write.
            legacy = s.get(Setting, "updates") if item is None else None
            return dict(item.value) if item else dict(legacy.value) if legacy else {}

    def store(self, value):
        with self.db() as s:
            item = s.get(Observation, "updates.release")
            if item is None:
                s.add(Observation(key="updates.release", value=value, source="release", observed_at=time.time()))
            else:
                item.value, item.observed_at = value, time.time()
            legacy = s.get(Setting, "updates")
            if legacy is not None:
                s.delete(legacy)
            s.commit()

    def audit(self, action, actor, target="", detail=None):
        with self.db() as s:
            s.add(AuditLog(actor=actor, action=action, target=target, detail=detail or {}))
            s.commit()

    def helper_state(self):
        try:
            return read_json(self.settings.updates_dir / "state.json")
        except (OSError, ValueError):
            return {}

    def state(self):
        value = self.observation()
        value["available"] = available(value, __version__, self.build)
        helper = self.helper_state()
        git = (self.settings.update_source / ".git").exists()
        config_error = None
        try:
            config = self.config()
        except ConfigError as error:
            config, config_error = self.defaults(), str(error)
            config["repository"] = ""
        selected = config["repository"]
        policy_block = (
            "Authorize this repository with ACC_UPDATE_REPOSITORY in root-owned /etc/default/accd and restart accd before installing updates."
            if selected and selected != self.authorized_repository()
            else None
        )
        return {
            **value,
            **config,
            "config_error": config_error,
            "version": __version__,
            "build": self.build,
            "git_checkout": git,
            "blocked": config_error or (GIT_MESSAGE if git else policy_block),
            "helper": (self.settings.updates_dir / "requests").is_dir(),
            "progress": helper,
            "pending": any((self.settings.updates_dir / "requests").glob("*.json")),
            "session_notice": "Agents keep running in tmux. Terminals disconnect briefly while the dashboard restarts.",
            "timezone": str(datetime.now().astimezone().tzinfo),
        }

    async def check(self, actor="system"):
        async with self.lock:
            value = self.observation()
            try:
                repo = self.config()["repository"]
            except ConfigError:
                return self.state()
            if not repo:
                return self.state()
            try:
                latest = await asyncio.to_thread(
                    check, repo, __version__, self.settings.data_dir / "secrets/update_token", self.build
                )
                value.update({k: v for k, v in latest.items() if k != "assets"}, error=None)
            except (ValueError, OSError) as error:
                # Never propagate network exception text: it may contain a credential or a private URL.
                generic = "Release check failed. Check the repository and token file, then try again."
                message = SOURCE_ERRORS.get(error.kind, generic) if isinstance(error, ReleaseSourceError) else generic
                value.update(error=message, available=False)
            value["checked_at"] = time.time()
            self.store(value)
            self.audit(
                "update.check",
                actor,
                value.get("latest", ""),
                {"version": __version__, "error": bool(value.get("error"))},
            )
            if value.get("available"):
                with self.db() as s:
                    key = f"update:{value['latest']}"
                    if not s.scalar(select(AttentionItem).where(AttentionItem.dedupe_key == key)):
                        s.add(
                            AttentionItem(
                                dedupe_key=key,
                                kind="update",
                                severity="info",
                                source="release",
                                title=f"Rynhult {value['latest']} is available",
                                detail="Open Settings → Updates and choose Back up and update.",
                                owner=None,
                            )
                        )
                    s.commit()
            return self.state()

    def request(self, actor):
        state = self.state()
        if state["blocked"]:
            raise ValueError(state["blocked"])
        if not self.config()["repository"] or not state.get("available") or state.get("error"):
            raise ValueError("Check for a newer release first")
        if not state["helper"]:
            raise ValueError("Install the acc-update helper with deploy/install.sh")
        if state["pending"] or state["progress"].get("running"):
            raise ValueError("An update is already in progress")
        request_id = uuid.uuid4().hex
        fence(self.db)
        write_json(
            self.settings.updates_dir / "requests" / f"{request_id}.json",
            {"action": "update", "version": state["latest"]},
        )
        self.audit("update.request", actor, state["latest"], {"from_version": __version__, "version": state["latest"]})
        return {"queued": True, "id": request_id}

    def bundle_descriptor(self):
        path = self.settings.bundle_dir / f"acc-{__version__}.tar.gz"
        return {"version": __version__, "build": self.build, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    def request_node(self, name, actor):
        link = self.ctx.broker.hub.get(name)
        if link.info.get("os") == "darwin":
            raise ValueError("Update this Mac with the new installer")
        supported = "update" in link.info.get("capabilities", []) and bool(BUILD.fullmatch(str(link.info.get("build", ""))))
        requested = time.time()
        with self.db() as s:
            node = s.get(Node, link.node_id)
            if node:
                node.info = {**(node.info or {}), "update": {
                    "requested_at": requested, "awaiting_result": supported, "running": supported,
                    **({} if supported else {"ok": False, "installer_required": True, "detail": NODE_INSTALLER_MESSAGE})}}
                s.commit()
        if not supported:
            return {"queued": False, "error": NODE_INSTALLER_MESSAGE}
        descriptor = self.bundle_descriptor()
        link.update_requested_at = requested
        link.queue(json.dumps({"t": "update", **descriptor}))
        self.audit("update.request", actor, __version__, {"machine": name, "version": __version__, "build": self.build})
        return {"queued": True}

    def follow_enabled(self, name):
        try:
            return self.config()["follow"].get(name, False)
        except ConfigError:
            return False  # corrupt updater preferences must not break machine connections

    def follow(self, link):
        name = link.name
        if (
            self.follow_enabled(name)
            and link.info.get("os") != "darwin"
            and link.info.get("build") != self.build
            and self.last_follow.get(name) != (link.connected_at, self.build)
        ):
            try:
                self.request_node(name, "system")
                self.last_follow[name] = (link.connected_at, self.build)
            except (ValueError, OSError, ConnectionError):
                pass

    async def tick(self):
        with self.db() as s:
            for node in s.scalars(select(Node).where(Node.status == "active")):
                node_update_state(node)
            s.commit()
        try:
            config = self.config()
        except ConfigError:
            return
        for link in list(self.ctx.broker.hub.links.values()):
            self.follow(link)
        value = self.observation()
        if config["repository"] and time.time() - value.get("checked_at", 0) >= 86400:
            await self.check()
            value = self.observation()
        value["available"] = available(value, __version__, self.build)
        helper = self.helper_state()
        if helper.get("finished_at") and helper.get("id") != value.get("result_id"):
            self.audit(
                "update.result",
                "system",
                helper.get("version", ""),
                {k: helper[k] for k in ("from_version", "version", "ok", "rolled_back") if k in helper},
            )
            if helper.get('ok') is False:
                self.ctx.supervisor.telemetry.capture(RuntimeError('Rynhult update failed'), 'update_failed', 'update')
            value["result_id"] = helper["id"]
            self.store(value)
        now = datetime.now().astimezone()
        day = now.date().isoformat()
        if (
            config["automatic"]
            and config["repository"]
            and value.get("available")
            and not value.get("error")
            and now.strftime("%H:%M") >= config["time"]
            and value.get("automatic_day") != day
        ):
            value["automatic_day"] = day
            self.store(value)
            try:
                self.request("system")
            except ValueError:
                pass

    async def run(self):
        while True:
            try:
                await self.tick()
            except Exception:
                log.warning("Updater tick failed; will retry")
            await asyncio.sleep(30)
