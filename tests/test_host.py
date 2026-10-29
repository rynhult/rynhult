"""Host pressure rules and the providers view (M9)."""
from __future__ import annotations

import json
import time

from helpers import write_db
from test_remote_spool import sup  # noqa: F401

from acc.server import host
from acc.server.models import AttentionItem


def snapshot(**overrides):
    base = {"collected_at": time.time(), "disks": [{"mount": "/srv", "device": "/dev/x", "total": 100, "used": 10,
            "free": 90e9, "percent": 10.0}], "smart": [{"device": "nvme0n1", "model": "m", "passed": True, "media_errors": 0}],
            "services": {"failed": []}, "gpu": [{"name": "T1000", "temperature": 45.0}]}
    base.update(overrides)
    return base


CALM = {"memory": {"percent": 30.0}}


async def test_host_refresh_waits_for_a_concurrent_writer(sup, monkeypatch):  # noqa: F811
    import asyncio
    import threading

    from sqlalchemy import event, text

    from acc.server.db import WORKER_WRITE, WRITE_INTENT

    monkeypatch.setattr(host, "live", lambda: CALM)
    engine = sup.db.kw["bind"]
    attempted = threading.Event()

    def beginning(connection, record, proxy):
        if WORKER_WRITE.get():
            attempted.set()

    token = WRITE_INTENT.set(True)
    try:
        writer = engine.connect()
        writer.begin()
    finally:
        WRITE_INTENT.reset(token)
    try:
        writer.execute(text("INSERT INTO observation (key, value, source, observed_at) "
                            "VALUES ('concurrent', '{}', 'test', 0)"))
        event.listen(engine, "checkout", beginning)
        task = asyncio.create_task(asyncio.to_thread(sup.refresh_host))
        try:
            assert await asyncio.wait_for(asyncio.to_thread(attempted.wait, 2), 3)
            writer.commit()
            await asyncio.wait_for(task, 3)
        finally:
            writer.rollback()
            await asyncio.gather(task, return_exceptions=True)
            event.remove(engine, "checkout", beginning)
    finally:
        writer.close()
    assert open_keys(sup.db)["host:collector"] == "warn"


async def test_model_refresh_waits_for_a_concurrent_writer(sup):  # noqa: F811
    import asyncio
    import threading

    from sqlalchemy import event, text

    from acc.server.db import WORKER_WRITE, WRITE_INTENT
    from acc.server.models import Observation

    catalog = {"fetched_at": 123, "claude": [{"id": "demo"}]}

    class Models:
        async def call(self, method):
            assert method == "model_catalog"
            return catalog

    sup.broker = Models()
    engine = sup.db.kw["bind"]
    attempted = threading.Event()

    def beginning(connection, record, proxy):
        if WORKER_WRITE.get():
            attempted.set()

    token = WRITE_INTENT.set(True)
    try:
        writer = engine.connect()
        writer.begin()
    finally:
        WRITE_INTENT.reset(token)
    try:
        writer.execute(text("INSERT INTO observation (key, value, source, observed_at) "
                            "VALUES ('concurrent', '{}', 'test', 0)"))
        event.listen(engine, "checkout", beginning)
        task = asyncio.create_task(sup.refresh_models())
        try:
            assert await asyncio.wait_for(asyncio.to_thread(attempted.wait, 2), 3)
            writer.commit()
            assert await asyncio.wait_for(task, 3) == catalog
        finally:
            writer.rollback()
            await asyncio.gather(task, return_exceptions=True)
            event.remove(engine, "checkout", beginning)
    finally:
        writer.close()
    with sup.db() as db:
        assert db.get(Observation, "models").value == catalog


def open_keys(db):
    with db() as s:
        return {i.dedupe_key: i.severity for i in s.query(AttentionItem).filter_by(resolved_at=None)}


def run(api, snap, current=CALM):
    db = api.app.state.db
    with write_db(db) as s:
        host.evaluate(s, snap, current)
        s.commit()
    return open_keys(db)


def test_calm_host_raises_nothing(api):
    assert not [k for k in run(api, snapshot()) if k.startswith("host:")]


def test_pressure_raises_and_recovery_resolves(api):
    keys = run(api, snapshot(disks=[{"mount": "/srv", "device": "d", "total": 100, "used": 96, "free": 4e9, "percent": 96.0}],
                             smart=[{"device": "sda", "model": "m", "passed": False, "media_errors": 0}],
                             services={"failed": ["foo.service"]}, gpu=[{"name": "T1000", "temperature": 90.0}]),
               {"memory": {"percent": 95.0}})
    assert keys["host:disk:/srv"] == "critical" and keys["host:smart:sda"] == "critical"
    assert {"host:failed-units", "host:gpu:0", "host:memory"} <= set(keys)
    healthy = snapshot(smart=[{"device": "sda", "model": "m", "passed": True, "media_errors": 0}])
    assert not [k for k in run(api, healthy) if k.startswith("host:")]  # a vanished disk would keep its alert


def test_stale_snapshot_is_flagged(api):
    assert "host:collector" in run(api, snapshot(collected_at=time.time() - 3600))
    assert "host:collector" in run(api, None)


async def test_host_endpoint_combines_snapshot_and_live(api, env):
    env["settings"].hoststate_path.write_text(json.dumps(snapshot()))
    view = (await api.get("/api/host")).json()
    assert view["snapshot"]["disks"][0]["mount"] == "/srv"
    assert 0 <= view["live"]["cpu_percent"] <= 100 and view["live"]["memory"]["total"] > 0


async def test_providers_view(api):
    view = (await api.get("/api/providers?refresh=1")).json()
    codex = view["observations"]["codex.rate_limits"]["value"]
    assert codex["plan"] == "prolite" and codex["primary"]["used_percent"] == 12 and codex["secondary"]["window_minutes"] == 10080
    assert view["links"]["claude"].startswith("https://claude.ai") and view["not_exposed"]


async def test_host_get_does_not_write_while_another_writer_is_active(api, monkeypatch):
    import asyncio

    from sqlalchemy import text

    await api.get("/api/health")
    api.supervisor_task.cancel()
    await asyncio.gather(api.supervisor_task, return_exceptions=True)
    await asyncio.gather(*api.supervisor_threads, return_exceptions=True)
    monkeypatch.setattr(host, "live", lambda: {"memory": {"percent": 95.0}})
    db = api.app.state.db
    with db() as writer:
        writer.execute(text("UPDATE attention_item SET title = title"))
        response = await asyncio.wait_for(api.get("/api/host"), timeout=2)
        assert response.status_code == 200
        assert response.json()["live"]["memory"]["percent"] == 95.0
        writer.rollback()


async def test_explicit_get_refresh_uses_cache_when_writer_is_active(api):
    import asyncio

    from sqlalchemy import text

    # Seed the same cache a periodic refresh maintains.
    paths = ["/api/providers?refresh=1", "/api/models?refresh=1", "/api/accounts?refresh=1"]
    for path in paths:
        assert (await api.get(path)).status_code == 200
    api.supervisor_task.cancel()
    await asyncio.gather(api.supervisor_task, return_exceptions=True)
    await asyncio.gather(*api.supervisor_threads, return_exceptions=True)
    with api.app.state.db() as writer:
        writer.execute(text("UPDATE attention_item SET title = title"))
        for path in paths:
            response = await api.get(path)
            assert response.status_code == 200
            assert response.json()
        writer.rollback()
