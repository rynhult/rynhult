"""Usage budgets: opt-in, per person; nothing exists or applies until someone sets one."""
from __future__ import annotations

import time

from helpers import verified_post

from acc.server import budgets
from acc.server.db import WRITE_INTENT
from acc.server.models import Observation, Setting

DAY = 86400


def seed(api, used=85.0, resets_in=3 * DAY):
    intent = WRITE_INTENT.set(True)
    with api.app.state.db() as s:
        s.merge(Observation(key="claude.rate_limits", source="test", observed_at=time.time(), value={
            "five_hour": {"used_percentage": 10, "resets_at": time.time() + 3600},
            "seven_day": {"used_percentage": used, "resets_at": time.time() + resets_in}}))
        s.commit()
    WRITE_INTENT.reset(intent)


def test_allowed_cap_pace_and_extra():
    week = {"resets_at": 1_000_000 + 7 * DAY, "length": 7 * DAY}
    now = 1_000_000 + 3.5 * DAY  # half way through the window
    assert budgets.allowed({"mode": "cap", "limit": 80}, week, now) == 80
    assert budgets.allowed({"mode": "pace", "limit": 100}, week, now) == 50
    assert budgets.allowed({"mode": "pace", "limit": 60}, week, now) == 30
    assert budgets.allowed({"mode": "cap", "limit": 80, "extra": 5, "extra_until": now + 1}, week, now) == 85
    assert budgets.allowed({"mode": "cap", "limit": 80, "extra": 5, "extra_until": now - 1}, week, now) == 80


async def test_nothing_until_someone_sets_a_budget(api):
    seed(api)
    state = (await api.get("/api/budgets")).json()
    assert state["rules"] == {} and state["status"] == {}
    assert {w["id"] for w in state["windows"]} == {"claude.five_hour", "claude.seven_day"}
    with api.app.state.db() as s:
        assert s.query(Setting).filter(Setting.key.like("budget.%")).count() == 0
    await api.app.state.supervisor.evaluate_budgets()
    assert not [i for i in (await api.get("/api/attention")).json() if i["kind"] == "budget"]


async def test_notify_pause_extra_and_clear(api, project):
    seed(api, used=85)
    bad = await api.put("/api/budgets", json={"rules": {"claude.seven_day": {"mode": "cap", "limit": 150}}})
    assert bad.status_code == 422
    state = (await api.put("/api/budgets", json={"rules": {"claude.seven_day": {"mode": "cap", "limit": 80, "action": "pause"}}})).json()
    assert state["status"]["claude.seven_day"] == {"allowed": 80.0, "used": 85.0, "over": True, "action": "pause",
                                                    "stale": False}
    item = next(i for i in (await api.get("/api/attention")).json() if i["kind"] == "budget")
    assert "past your 80% budget" in item["title"]

    launch = {"project_id": project["id"], "provider": "claude", "name": "over"}
    refused = await api.post("/api/sessions", json=launch)
    assert refused.status_code == 409 and "budget pauses new Claude Code launches" in refused.text
    codex = await api.post("/api/sessions/preview", json={**launch, "provider": "codex"})
    assert codex.status_code == 200  # other agents aren't affected
    assert (await api.post("/api/sessions", json={**launch, "budget_ok": True})).status_code == 201

    lifted = (await api.post("/api/budgets/extra", json={"window": "claude.seven_day"})).json()
    assert lifted["status"]["claude.seven_day"]["allowed"] == 85.0  # +5% until the window resets
    seed(api, used=84)
    await api.app.state.supervisor.evaluate_budgets()
    assert not [i for i in (await api.get("/api/attention")).json() if i["kind"] == "budget"]

    cleared = (await api.put("/api/budgets", json={"rules": {}})).json()
    assert cleared["rules"] == {}
    with api.app.state.db() as s:
        assert s.get(Setting, "budget.casey") is None and s.query(Setting).filter(Setting.key.like("budget.%")).count() == 0


async def test_demoted_owner_sees_no_usage_and_their_budget_stops_applying(api, env, member_accounts):
    """R1: a demoted owner's budget is deleted, and nothing reads (or acts on) the owners' plan usage for them."""
    from test_auth import do_setup
    from test_users import browser, csrf, join

    await do_setup(api, env)
    seed(api, used=90)
    link = (await verified_post(api, "/api/users/invites", {"role": "owner", "account": "alice-agent"})).json()["link"]
    alice = browser(api)
    try:
        assert (await join(alice, link, "alice")).status_code == 200
        rule = {"claude.seven_day": {"mode": "cap", "limit": 80, "action": "pause"}}
        state = (await alice.put("/api/budgets", json={"rules": rule}, headers=csrf(alice))).json()
        assert state["status"]["claude.seven_day"]["used"] == 90.0
        assert [i for i in (await alice.get("/api/attention")).json() if i["kind"] == "budget"]

        users = (await api.get("/api/users")).json()["users"]
        user_id = next(u["id"] for u in users if u["username"] == "alice")
        assert (await api.post(f"/api/users/{user_id}/role", json={"role": "member"})).status_code == 200

        state = (await alice.get("/api/budgets")).json()
        assert state["windows"] == [] and state["status"] == {}
        assert state["rules"] == {}  # deleted: budgets only apply to owners
        with api.app.state.db() as s:
            from acc.server.models import AuditLog
            assert s.query(AuditLog).filter(AuditLog.action == "user.role").all()[-1].detail["budget_deleted"] is True
        extra = await alice.post("/api/budgets/extra", json={"window": "claude.seven_day"}, headers=csrf(alice))
        assert extra.status_code == 403
        with api.app.state.db() as s:
            assert budgets.status(s, "alice") == {} and budgets.paused(s, "alice", "claude") == []
        await api.app.state.supervisor.evaluate_budgets()
        with api.app.state.db() as s:
            from acc.server.models import AttentionItem
            open_items = s.query(AttentionItem).filter(AttentionItem.kind == "budget", AttentionItem.resolved_at.is_(None)).all()
            assert open_items == []
    finally:
        await alice.aclose()


async def test_a_window_past_its_reset_neither_pauses_nor_alerts(api, project):
    """R3: a reading from before the reset is stale: no pause, no alert, no +5%, until a fresh reading arrives."""
    seed(api, used=90)
    rule = {"claude.seven_day": {"mode": "cap", "limit": 80, "action": "pause"}}
    assert (await api.put("/api/budgets", json={"rules": rule})).json()["status"]["claude.seven_day"]["over"]
    assert [i for i in (await api.get("/api/attention")).json() if i["kind"] == "budget"]

    seed(api, used=90, resets_in=-60)  # the window reset a minute ago; no new reading yet
    state = (await api.get("/api/budgets")).json()
    week = next(w for w in state["windows"] if w["id"] == "claude.seven_day")
    assert week["stale"] and state["status"]["claude.seven_day"]["over"] is False
    assert state["status"]["claude.seven_day"]["stale"] is True
    assert "claude.seven_day" in state["rules"]  # the rule waits for the next window
    with api.app.state.db() as s:
        assert budgets.paused(s, "@api", "claude") == []
    launch = {"project_id": project["id"], "provider": "claude", "name": "after-reset"}
    assert (await api.post("/api/sessions", json=launch)).status_code == 201
    await api.app.state.supervisor.evaluate_budgets()
    assert not [i for i in (await api.get("/api/attention")).json() if i["kind"] == "budget"]
    assert (await api.post("/api/budgets/extra", json={"window": "claude.seven_day"})).status_code == 422

    seed(api, used=90)  # a fresh reading over the cap: enforced again
    with api.app.state.db() as s:
        assert budgets.paused(s, "@api", "claude") == ["Claude 7-day window"]
