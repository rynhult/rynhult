"""Request size, query limits and odd input: refused cleanly, never a 500 or an unbounded read."""
from __future__ import annotations

from helpers import wait_for


async def test_oversized_bodies_refused_before_auth(api):
    big = b'{"x": "' + b"a" * (5 << 20) + b'"}'
    response = await api.post("/api/sessions", content=big, headers={"content-type": "application/json", "cookie": ""})
    assert response.status_code == 413

    async def chunks():
        for _ in range(6):
            yield b"a" * (1 << 20)
    streamed = await api.post("/api/sessions", content=chunks(), headers={"content-type": "application/json"})
    assert streamed.status_code == 413


async def test_negative_limits_do_not_lift_the_cap(api, project):
    for path in ("/api/audit", "/api/attention"):
        assert len((await api.get(path, params={"limit": -1})).json()) <= 1


async def test_malformed_push_endpoint_is_a_422_not_a_500(api):
    response = await api.post("/api/push/subscribe", json={"endpoint": "https://[fcm.googleapis.com]/x",
                                                           "keys": {"p256dh": "x", "auth": "y"}})
    assert response.status_code in (400, 422)


async def test_non_ascii_session_name_gets_a_usable_tmux_name(api, project):
    created = (await api.post("/api/sessions", json={"project_id": project["id"], "provider": "claude", "name": "Ωmega"})).json()
    assert created["tmux_name"].startswith("acc-mega-") or created["tmux_name"].startswith("acc-")
    assert created["tmux_name"].isascii()

    async def launched():
        return (await api.get(f"/api/sessions/{created['id']}")).json()["observed_state"] not in ("starting", "recovering")
    assert await wait_for(launched)
