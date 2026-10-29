"""Tools are requested by sessions, approved by people, and executed only in their derived scope."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update

from .. import tooling
from . import attention
from .broker_client import BrokerError
from .ha import fence
from .models import API_PRINCIPAL, AuditLog, InstalledTool, Session, Setting, ToolRequest, User
from .node_tasks import NodeTasks

log = logging.getLogger("acc.tools")


class ToolRefused(ValueError):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def required(db, owner: str | None, project_id: int | None) -> bool:
    for key in (f"tools.person.{owner}", f"tools.project.{project_id}"):
        setting = db.get(Setting, key)
        if setting and setting.value.get("required") is False:
            return False
    return True


def out(row) -> dict:
    return {c.name: getattr(row, c.name) for c in row.__table__.columns} | {
        "destination": tooling.destination(row.manager, row.account)}


class Tools:
    def __init__(self, settings, db, brokers):
        self.settings, self.db, self.brokers = settings, db, brokers
        self.timeout = 900
        self.tasks: set[asyncio.Task] = set()
        self.local_tasks = NodeTasks("local-tools")
        with db() as s:
            interrupted = list(s.scalars(select(ToolRequest.id).where(ToolRequest.status.in_(("approved", "applying")))))
        for rid in interrupted:
            self._result(rid, {"ok": False, "detail": "Rynhult restarted during the change; inspect the machine before retrying"})

    @staticmethod
    def _role(db, username: str) -> str | None:
        return "owner" if username == API_PRINCIPAL else db.scalar(select(User.role).where(User.username == username))

    def _valid_person(self, db, r):
        role = self._role(db, r.owner)
        user = db.scalar(select(User).where(User.username == r.owner))
        if role is None or role == "member" and (
                r.manager in tooling.SYSTEM or user.account != r.account):
            raise ToolRefused("the requester's role or agent account changed; ask again")
        if r.session_id:
            session = db.get(Session, r.session_id)
            if session is None or session.owner != r.owner or session.run_as != r.account or session.host != r.host:
                raise ToolRefused("the session's person, account or machine changed; ask again")

    def _new(self, db, *, owner, account, host, manager, name, version, operation, reason,
             session_id=None, project_id=None) -> str:
        tooling.validate(manager, name, version)
        if operation not in tooling.OPERATIONS or not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
            raise ToolRefused("use install/remove and a reason of 1-500 characters", 422)
        if self._role(db, owner) is None:
            raise ToolRefused("the session has no active person", 403)
        if manager in tooling.SYSTEM and self._role(db, owner) != "owner":
            raise ToolRefused("only owners may request system-wide tools", 403)
        r = ToolRequest(id=f"tool-{uuid.uuid4().hex[:12]}", owner=owner, account=account, host=host,
                        manager=manager, name=name, version=version, operation=operation, reason=reason,
                        session_id=session_id, project_id=project_id)
        self._valid_person(db, r)
        duplicate = db.scalar(select(ToolRequest.id).where(
            ToolRequest.host == host, ToolRequest.account == account if manager not in tooling.SYSTEM else True,
            ToolRequest.manager == manager,
            ToolRequest.name == name, ToolRequest.status.in_(("pending_approval", "approved", "applying"))))
        if duplicate:
            raise ToolRefused("this tool already has a change waiting")
        db.add(r)
        db.flush()
        detail = (f"{manager}: {name} {version or 'latest'}; {host}, {tooling.destination(manager, account)}. {reason}")
        attention.raise_item(db, f"tool:{r.id}", kind="tool_request", source="tools", session_id=session_id,
                             owner="" if manager in tooling.SYSTEM else owner,
                             title=f"Tool request: {operation} {name}", detail=detail)
        db.add(AuditLog(actor=owner, action="tool.request", target=r.id, detail={
            "manager": manager, "name": name, "version": version, "operation": operation, "host": host, "account": account}))
        return r.id

    def request_session(self, session_id: str, *, manager, name, version=None, action="install", reason) -> str:
        with self.db() as db:
            session = db.get(Session, session_id)
            if session is None or session.management != "acc":
                raise ToolRefused("no managed session")
            rid = self._new(db, owner=session.owner or API_PRINCIPAL, account=session.run_as, host=session.host,
                            session_id=session.id, project_id=session.project_id, manager=manager, name=name,
                            version=version, operation=action, reason=reason)
            db.commit()
            return rid

    def remove(self, tool_id: str, actor: str) -> str:
        with self.db() as db:
            tool = db.get(InstalledTool, tool_id)
            if tool is None or not self.can_see(db, tool, actor):
                raise ToolRefused("no such tool", 404)
            rid = self._new(db, owner=actor, account=tool.account, host=tool.host, manager=tool.manager,
                            name=tool.name, version=None, operation="remove", reason=f"Remove requested by {actor}")
            db.commit()
            return rid

    def can_see(self, db, r, actor: str) -> bool:
        if self._role(db, actor) == "owner":
            return True
        user = db.scalar(select(User).where(User.username == actor))
        return bool(user and r.owner == actor and r.account == user.account and r.manager not in tooling.SYSTEM)

    def decide(self, rid: str, actor: str, approve: bool):
        with self.db() as db:
            r = db.get(ToolRequest, rid)
            if r is None or not self.can_see(db, r, actor):
                raise ToolRefused("no such request", 404)
            if approve:
                self._valid_person(db, r)
            status = "approved" if approve else "rejected"
            claimed = db.execute(update(ToolRequest).where(ToolRequest.id == rid,
                                 ToolRequest.status == "pending_approval").values(status=status, decided_by=actor))
            if not claimed.rowcount:
                raise ToolRefused("request is no longer pending")
            attention.resolve(db, f"tool:{rid}")
            db.add(AuditLog(actor=actor, action="tool.approve" if approve else "tool.reject", target=rid))
            host = r.host
            db.commit()
        if approve:
            if host != "local" and self.brokers.hub is not None:
                task = self.brokers.hub.start(host, self.execute(rid), f"tool:{rid}", report=True)
            else:
                task = self.local_tasks.start(self.execute(rid), f"tool:{rid}")
            self.tasks.add(task)
            def completed(done):
                self.tasks.discard(done)
                if done.cancelled():
                    try:
                        with self.db() as db:
                            row = db.get(ToolRequest, rid)
                            interrupted = row and row.status in ("approved", "applying")
                        if interrupted:
                            self._result(rid, {"ok": False, "detail": "Machine work was interrupted; inspect the machine before retrying"})
                    except Exception:
                        log.exception("tool %s: could not record interruption", rid)
            task.add_done_callback(completed)

    async def execute(self, rid: str):
        refusal = None
        with self.db() as db:
            claimed = db.execute(update(ToolRequest).where(ToolRequest.id == rid,
                                 ToolRequest.status == "approved").values(status="applying"))
            if not claimed.rowcount:
                return
            db.commit()
            request = db.get(ToolRequest, rid)
            try:
                self._valid_person(db, request)
            except ToolRefused as error:
                refusal = str(error)
            db.expunge(request)
        if refusal is not None:
            self._result(rid, {"ok": False, "detail": refusal})
            return
        try:
            result = await self._carry(request)
            if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
                raise ToolRefused("invalid tool executor result")
            if result["ok"] and request.operation == "install":
                version = result.get("version")
                if not isinstance(version, str) or not version or len(version) > 100:
                    raise ToolRefused("executor did not report an installed version")
                if request.version is not None and version != request.version:
                    raise ToolRefused("executor reported a different installed version")
        except (ValueError, OSError, TimeoutError, BrokerError, ConnectionError) as error:
            log.warning("tool %s failed: %s", rid, error)
            result = {"ok": False, "detail": str(error)}
        self._result(rid, result)

    async def _carry(self, r):
        fence(self.db)  # background work: its approval's request has already drained
        if r.manager not in tooling.SYSTEM:
            client = self.brokers.fresh(r.account, r.host, timeout=self.timeout + 30)
            try:
                return await client.call("tool_change", manager=r.manager, name=r.name,
                                         version=r.version, operation=r.operation)
            finally:
                await client.close()
        payload = {"id": r.id, "action": "tool", "manager": r.manager, "name": r.name,
                   "version": r.version, "operation": r.operation}
        if r.host != "local":
            return await self.brokers.hub.get(r.host).tool(payload, timeout=self.timeout)
        root = self.settings.packages_dir
        tooling.helper_request(payload)
        tmp = root / "requests" / f".{r.id}.tmp"
        tmp.write_text(json.dumps(payload))
        os.chmod(tmp, 0o640)
        tmp.replace(root / "requests" / f"{r.id}.json")
        for _ in range(int(self.timeout / 0.25)):
            path = root / "results" / f"{r.id}.json"
            if path.exists():
                return json.loads(path.read_text())
            await asyncio.sleep(0.25)
        raise TimeoutError("acc-packages did not answer; inspect the machine before retrying")

    def _result(self, rid: str, result: dict):
        with self.db() as db:
            r = db.get(ToolRequest, rid)
            r.status = ("applied" if r.operation == "install" else "removed") if result["ok"] else "failed"
            r.detail = str(result.get("detail") or "")[:1000]
            r.resolved_version = result.get("version") if result["ok"] else None
            r.completed_at = datetime.now(timezone.utc)
            account = "" if r.manager in tooling.SYSTEM else r.account
            key = hashlib.sha256(json.dumps([r.host, account, r.manager, r.name]).encode()).hexdigest()
            if result["ok"]:
                old = db.get(InstalledTool, key)
                if r.operation == "remove":
                    if old:
                        db.delete(old)
                else:
                    db.merge(InstalledTool(id=key, owner=r.owner, host=r.host, account=account, manager=r.manager,
                                           name=r.name, version=r.resolved_version, request_id=rid))
            else:
                attention.raise_item(db, f"tool_failed:{rid}", kind="tool_failed", source="tools",
                                     session_id=r.session_id, owner="" if r.manager in tooling.SYSTEM else r.owner,
                                     title=f"Couldn't {r.operation} the tool {r.name}", detail=r.detail)
            db.add(AuditLog(actor=r.decided_by or "system", action="tool.result", target=rid,
                            detail={"ok": result["ok"], "version": r.resolved_version, "detail": r.detail}))
            db.commit()
