"""The status board's one route (docs/STATUS-BOARD.md): machines with their summaries and alerts, projects with their
machine and backup state; owners only, and a read that asks no machine for anything."""
from __future__ import annotations

import asyncio
import json
import time

from helpers import wait_for, write_db
from test_auth import live  # noqa: F401 (fixture)
from test_member_views import member  # noqa: F401 (fixture)
from test_multihost import enroll
from test_node_host import connect, snapshot

from acc.server import attention
from acc.server.models import Observation, Project


async def test_board_lists_this_machine_nodes_and_projects(api, env, live):  # noqa: F811
    env["settings"].hoststate_path.write_text(json.dumps(snapshot(disks=[
        {"mount": "/srv", "device": "/dev/x", "total": 100, "used": 50, "free": 50e9, "percent": 50.0}])))
    made = await api.post("/api/projects", json={"slug": "demo-api", "name": "Demo API", "workspace_path": str(env["workspaces"] / "demo-api")})
    assert made.status_code == 201, made.text
    pid = made.json()["id"]
    with write_db(api.app.state.db) as s:
        s.get(Project, pid).git_backup = True
        s.merge(Observation(key=f"backup.{pid}", value={"last_at": "2026-10-23T08:00:00+00:00", "checked_at": time.time()},
                            source="supervisor", observed_at=time.time()))
        attention.raise_item(s, "host:memory", kind="host", severity="warn", source="psutil", title="Memory 95% used", detail="")
        s.commit()
    key, node_id = await enroll(api)
    answers: list[str] = []
    task, ready = await connect(live, key, node_id, ["preview-v1", "host-v1"], answers)
    try:
        await asyncio.wait_for(ready.wait(), 5)
        await wait_for(lambda: api.app.state.ctx.broker.hub.get("pc").host_light is not None)
        board = (await api.get("/api/board")).json()
        local, pc = board["machines"]
        assert local["local"] and local["connected"] and local["light"]["disk"]["mount"] == "/srv"
        assert {"severity": "warn", "title": "Memory 95% used"} in local["alerts"]
        assert pc["name"] == "pc" and pc["connected"] and pc["supported"] and pc["version"] == "0.12.0"
        assert pc["light"]["cpu_percent"] == 7.0 and pc["alerts"] == [] and not pc["stale"]
        assert answers == []  # the board reads summaries; it never asks a machine for a full snapshot
        project = next(p for p in board["projects"] if p["id"] == pid)
        assert project["host"] == "local" and project["git_backup"] and project["backup"]["last_at"].startswith("2026-10-23")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    await wait_for(lambda: "pc" not in api.app.state.ctx.broker.hub.links)
    pc = (await api.get("/api/board")).json()["machines"][1]
    assert not pc["connected"] and pc["light"] is None and pc["alerts"] == []  # offline: nothing invented


async def test_a_full_node_raises_its_alerts_on_the_board(api, env, live):  # noqa: F811
    key, node_id = await enroll(api)
    task, ready = await connect(live, key, node_id, ["host-v1"], [])
    try:
        await asyncio.wait_for(ready.wait(), 5)
        link = api.app.state.ctx.broker.hub.get("pc")
        await wait_for(lambda: link.host_light is not None)
        link.host_state({"t": "host-light", "light": {"collected_at": time.time(), "cpu_percent": 5.0, "memory_percent": 93.0,
                                                      "disk": {"mount": "/", "percent": 97.0, "free": 1e9}}})
        pc = (await api.get("/api/board")).json()["machines"][1]
        assert [a["severity"] for a in pc["alerts"]] == ["critical", "warn"]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_the_board_is_for_owners(api, env, member):  # noqa: F811
    assert (await member.get("/api/board")).status_code == 403
