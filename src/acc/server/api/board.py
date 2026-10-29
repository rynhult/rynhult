"""The status board (owners): every machine and project at a glance, for a large screen (docs/STATUS-BOARD.md).

One read-only GET that the board polls. Sessions, the Inbox and plan usage come from their own routes (the queries
every page shares); this adds what no route has in one place: each machine's summary and alerts, and each project's
machine and backup state. Nothing here asks a machine for anything: nodes' numbers are their periodic summaries.
"""
from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Depends
from sqlalchemy import select

from ... import __version__, hostinfo
from ..machine_presence import planned_downtime
from ..models import AttentionItem, Node, Observation, Project
from ..node_host import alerts, light_from
from .context import Context

LIGHT_STALE = 180  # seconds: a node's summary older than this is shown as old (it sends one every minute)


def router(ctx: Context) -> APIRouter:
    api = APIRouter()
    db, supervisor = ctx.db, ctx.supervisor

    @api.get("/api/board")
    async def board(who=Depends(ctx.owner)):
        view = await asyncio.to_thread(supervisor.host_view)
        snapshot = view.get("snapshot")
        local_light = light_from({**(snapshot or {}), "live": view["live"]})
        local_light["collected_at"] = view["live"].get("measured_at") or time.time()
        with db() as s:
            host_alerts = [{"severity": a.severity, "title": a.title} for a in s.scalars(
                select(AttentionItem).where(AttentionItem.kind == "host", AttentionItem.resolved_at.is_(None))
                .order_by(AttentionItem.updated_at.desc()))]
            nodes = [{"name": n.name, "status": n.status, "info": dict(n.info or {}), "last_seen": n.last_seen, "planned_downtime": planned_downtime(n)}
                     for n in s.scalars(select(Node).where(Node.status == "active").order_by(Node.name))]
            projects = list(s.scalars(select(Project).where(Project.archived.is_(False)).order_by(Project.name)))
            backups = {o.key: o.value for o in s.scalars(select(Observation).where(
                Observation.key.in_([f"backup.{p.id}" for p in projects])))}
            project_rows = [{"id": p.id, "slug": p.slug, "name": p.name, "host": p.host or "local",
                             "git_backup": bool(p.git_backup), "backup": _backup(backups.get(f"backup.{p.id}"))}
                            for p in projects]
        machines = [{"name": "local", "label": ctx.settings.host_label, "local": True, "connected": True,
                     "version": __version__, "os": hostinfo.system(), "supported": True, "update_available": False,
                     "light": local_light, "light_at": local_light["collected_at"], "alerts": host_alerts}]
        hub = ctx.broker.hub
        now = time.time()
        for node in nodes:
            link = hub.links.get(node["name"]) if hub else None
            connected = bool(link and not link.closed)
            light = link.host_light if connected else None
            info = link.info if connected else node["info"]
            machines.append({
                "name": node["name"], "label": node["name"], "local": False, "connected": connected,
                "planned_downtime": node["planned_downtime"],
                "last_seen": node["last_seen"], "version": info.get("version") or node["info"].get("version"),
                "os": node["info"].get("os"), "supported": bool(connected and link.host_capable),
                "update_available": node["info"].get("build") != ctx.updates.build,
                "light": light, "light_at": link.host_light_at if connected else None,
                "stale": bool(light and now - (link.host_light_at or 0) > LIGHT_STALE),
                "alerts": alerts(light, link.host_snapshot) if connected else []})
        return {"generated_at": now, "machines": machines, "projects": project_rows}

    return api


def _backup(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    return {k: value.get(k) for k in ("last_at", "failed_at", "error", "checked_at", "note")}
