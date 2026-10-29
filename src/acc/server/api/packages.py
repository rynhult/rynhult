"""Agent software (owners): which CLIs and model servers are installed, their latest versions, and requests to the
acc-packages root helper to install, update or remove them or switch daily auto-updates. accd can't install anything
itself; the helper only accepts packages from its own allow-list (deploy/packages/acc-packages).

Every route takes `machine` (default: this one). For another machine the request goes over its link as a
"packages" text frame, and its acc-node (with that machine's own helper, or by itself on a Mac) reports back with
"packages-state" (docs/MULTIHOST.md, "Agent software")."""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..agent_setup import BACKUP_TOOLS, guide, system_of
from ..ha import fence
from ..models import AuditLog
from .context import Context

PACKAGES = ("restic", "claude", "codex", "copilot", "antigravity", "cursor", "opencode", "ollama")  # the helper's allow-list (it checks again)
ACTIONS = ("install", "update", "remove", "check", "auto")
REMOTE_ACTIONS = ("install", "update", "remove", "check")  # what a node's acc-node accepts
REQUEST_ID = re.compile(r"pkg-[0-9a-f]{12}")
MAC_INSTALLS = ("codex", "copilot")  # the catalog's mac_ok entries: what a Mac's acc-node installs itself
ACCOUNT = re.compile(r"[a-z_][a-z0-9_-]{0,31}")


def this_system() -> str:
    """The main Rynhult: Linux, or WSL on a Windows PC."""
    try:
        return "wsl" if "microsoft" in Path("/proc/sys/kernel/osrelease").read_text().lower() else "linux"
    except OSError:
        return "linux"


def annotate(packages: dict, kind: str, helper: bool, system: str, account: str | None, machine: str) -> dict:
    """Each entry gets its group (agent software or backup tools), whether Rynhult can install it on that machine now,
    and, when it can't, the steps to install it by hand there. The dashboard offers only what the machine accepts."""
    out = {}
    for name, entry in packages.items():
        installable = helper and (kind != "mac" or name in MAC_INSTALLS)
        steps = None if installable else guide(name, system, account, machine)
        out[name] = {**entry, "group": "backup" if name in BACKUP_TOOLS else "agent", "installable": installable,
                     **({"guide": steps} if steps else {})}
    return out


def refusal(name: str, action: str, kind: str | None, helper: bool, machine: str) -> str | None:
    """Why that machine would refuse `action` for `name` (the same rule its acc-node applies), or None. A Mac checks
    any CLI's version (one installed by hand too); it installs, updates and removes only its own npm ones."""
    label = (guide(name, "linux") or {}).get("label", name)
    if kind == "mac":
        if name in MAC_INSTALLS or action == "check":
            return None if helper or action == "check" else f"npm isn't installed on {machine}. Run brew install node there, then try again."
        return f"{label} can't be installed on a Mac from Rynhult. Install it on {machine} yourself; Providers shows the steps."
    if not helper:
        return f"{machine} can't install software from Rynhult yet. Update Rynhult there, or install {label} by hand."
    return None


def _text(value, limit: int) -> str | None:
    return value[:limit] if isinstance(value, str) else None


def _number(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def clean_state(message: dict) -> dict:
    """A node's "packages-state", kept only as far as it fits what the dashboard shows: catalog names, known fields,
    bounded strings. The node's word on its own software, nothing more."""
    packages = message.get("packages") if isinstance(message.get("packages"), dict) else {}
    out = {}
    for name in PACKAGES:
        entry = packages.get(name)
        if not isinstance(entry, dict):
            continue
        last = entry.get("last") if isinstance(entry.get("last"), dict) else None
        out[name] = {k: v for k, v in {
            "label": _text(entry.get("label"), 80), "method": _text(entry.get("method"), 20),
            "note": _text(entry.get("note"), 300), "installed": _text(entry.get("installed"), 64),
            "channel": _text(entry.get("channel"), 32), "upstream_latest": _text(entry.get("upstream_latest"), 64),
            "latest": _text(entry.get("latest"), 64), "checked_at": _number(entry.get("checked_at")),
            "auto_update": entry.get("auto_update") if isinstance(entry.get("auto_update"), bool) else None,
            "mac_ok": entry.get("mac_ok") if isinstance(entry.get("mac_ok"), bool) else None,
            "running": entry.get("running") if entry.get("running") in ACTIONS else None,
            "last": {"action": _text(last.get("action"), 20), "ok": last.get("ok") is True,
                     "detail": _text(last.get("detail"), 500) or "", "at": _number(last.get("at"))} if last else None,
        }.items() if v is not None}
    pending = message.get("pending") if isinstance(message.get("pending"), list) else []
    refused = message.get("refused") if isinstance(message.get("refused"), dict) else None
    return {
        "kind": message.get("kind") if message.get("kind") in ("helper", "mac") else "helper",
        "helper": message.get("helper") is True, "packages": out,
        "pending": [{"id": p["id"], "package": p["package"], "action": p["action"]} for p in pending[:16]
                    if isinstance(p, dict) and isinstance(p.get("id"), str) and REQUEST_ID.fullmatch(p["id"])
                    and p.get("package") in PACKAGES and p.get("action") in ACTIONS],
        "refused": {"id": _text(refused.get("id"), 20) or "", "detail": _text(refused.get("detail"), 300) or "",
                    "at": _number(refused.get("at"))} if refused else None,
        "daily_at": _number(message.get("daily_at")), "updated_at": _number(message.get("updated_at")),
    }


class AutoIn(BaseModel):
    enabled: bool


def router(ctx: Context) -> APIRouter:
    db, root = ctx.db, ctx.settings.packages_dir
    api = APIRouter(prefix="/api/packages")

    def audit(who, name: str, action: str, machine: str, auto: bool | None = None) -> None:
        with db() as s:
            s.add(AuditLog(actor=who.username, action=f"package.{action}", target=name,
                           detail={"machine": machine, **({"auto": auto} if auto is not None else {})}))
            s.commit()

    def queue(name: str, action: str, who, auto: bool | None = None, machine: str | None = None) -> dict:
        if name not in PACKAGES:
            raise HTTPException(404, "Rynhult doesn't know that tool.")
        link = ctx.machine(machine)
        request_id = f"pkg-{uuid.uuid4().hex[:12]}"
        if link is not None:
            if action not in REMOTE_ACTIONS:
                raise HTTPException(422, "Auto-update for that machine is set on the machine itself, not from here.")
            if link.packages is None:
                raise HTTPException(409, f"{link.name} hasn't reported its agent software yet. Update Rynhult there.")
            if name in BACKUP_TOOLS and link.name not in backup_machines():
                raise HTTPException(409, f"{link.name} isn't a backup target, so it doesn't need {name}.")
            why = refusal(name, action, link.packages.get("kind"), bool(link.packages.get("helper")), link.name)
            if why:
                raise HTTPException(409, why)
            link.request_packages(request_id, action, name)
            audit(who, name, action, link.name)
            return {"request_id": request_id, "queued": True}
        requests = root / "requests"
        if not requests.is_dir():
            raise HTTPException(503, "The acc-packages helper isn't installed on this machine. Run deploy/install.sh to add it.")
        fence(ctx.db)
        tmp = requests / f".{request_id}.tmp"
        tmp.write_text(json.dumps({"id": request_id, "package": name, "action": action,
                                   **({"auto": auto} if auto is not None else {})}))
        tmp.replace(requests / f"{request_id}.json")
        audit(who, name, action, "local", auto)
        return {"request_id": request_id, "queued": True}

    def backup_machines() -> set[str]:
        """Machines that hold a backup repository (restic belongs there); today's targets are all on this machine."""
        jobs = getattr(ctx, "backup_jobs", None)
        try:
            targets = jobs.service.targets() if jobs is not None else []
        except (OSError, ValueError):
            targets = []
        return {t["machine"] for t in targets if isinstance(t, dict) and t.get("kind") == "machine"
                and isinstance(t.get("machine"), str)}

    def remote_state(link, account: str | None) -> dict:
        data = link.packages or {}
        names = {name: (data.get("packages") or {}).get(name, {}) for name in PACKAGES
                 if name not in BACKUP_TOOLS or link.name in backup_machines()}
        account = account if account in link.accounts else (link.accounts[0] if len(link.accounts) == 1 else None)
        names = annotate(names, data.get("kind") or "helper", bool(data.get("helper")) and link.packages is not None,
                         system_of(link.info, data.get("kind")), account, link.name)
        return {"packages": names, "pending": data.get("pending", []), "daily_at": data.get("daily_at"),
                "updated_at": data.get("updated_at"), "helper": bool(data.get("helper")), "kind": data.get("kind"),
                "reported": link.packages is not None, "refused": data.get("refused"), "machine": link.name}

    @api.get("")
    def state(machine: str | None = None, account: str | None = None, who=Depends(ctx.owner)):
        account = account if account and ACCOUNT.fullmatch(account) else None
        link = ctx.machine(machine)
        if link is not None:
            return remote_state(link, account)
        try:
            data = json.loads((root / "state.json").read_text())
        except (OSError, ValueError):
            data = {"packages": {}}
        pending = []
        for path in (root / "requests").glob("*.json") if (root / "requests").is_dir() else ():
            try:
                pending.append(json.loads(path.read_text()))
            except (OSError, ValueError):
                continue
        packages = annotate({name: data.get("packages", {}).get(name, {}) for name in PACKAGES}, "local",
                            (root / "requests").is_dir(), this_system(), account or ctx.settings.default_account,
                            "this machine")
        return {"packages": packages, "pending": pending, "daily_at": data.get("daily_at"),
                "updated_at": data.get("updated_at"), "helper": (root / "requests").is_dir(), "kind": "local",
                "reported": True, "machine": "local"}

    @api.get("/requests/{request_id}")
    def result(request_id: str, machine: str | None = None, who=Depends(ctx.owner)):
        if not REQUEST_ID.fullmatch(request_id):
            raise HTTPException(404, "That request was not found.")
        link = ctx.machine(machine)
        if link is not None:
            sent = link.package_requests.get(request_id)
            data = link.packages or {}
            if sent is None:
                raise HTTPException(404, "That request was not found.")
            refused = data.get("refused") or {}
            if refused.get("id") == request_id:
                return {"done": True, "ok": False, "detail": refused.get("detail"), **sent}
            if any(p["id"] == request_id for p in data.get("pending", [])):
                return {"done": False, "queued": True}
            last = ((data.get("packages") or {}).get(sent["package"]) or {}).get("last") or {}
            if (last.get("at") or 0) >= sent["at"] - 5 and not (data.get("packages") or {}).get(sent["package"], {}).get("running"):
                return {"done": True, "ok": last.get("ok"), "detail": last.get("detail"), **sent}
            return {"done": False, "queued": True}
        path = root / "results" / f"{request_id}.json"
        if path.exists():
            return {"done": True, **json.loads(path.read_text())}
        return {"done": False, "queued": (root / "requests" / f"{request_id}.json").exists()}

    @api.post("/{name}/auto", status_code=202)
    def set_auto(name: str, body: AutoIn, machine: str | None = None, who=Depends(ctx.owner)):
        return queue(name, "auto", who, body.enabled, machine)

    @api.post("/{name}/{action}", status_code=202)
    async def request(name: str, action: str, machine: str | None = None, who=Depends(ctx.owner)):
        # async: a link's queue lives on the event loop
        if action not in REMOTE_ACTIONS:
            raise HTTPException(404, "Rynhult can't do that with this tool.")
        return queue(name, action, who, machine=machine)

    return api
