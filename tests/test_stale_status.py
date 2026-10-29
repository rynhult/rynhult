"""A silent hook is not evidence that an agent is still thinking."""
from datetime import datetime, timedelta, timezone

import pytest
from helpers import pause_supervisor, write_db

from acc.server.models import LaunchSpec, Project, Session


@pytest.mark.parametrize(("state", "source", "missing_times", "fresh", "stale"), [
    ("active", "claude_hook", False, False, True),
    ("active", "claude_hook", True, False, True),
    ("active", "claude_hook", False, True, False),
    ("active", "tmux", False, False, False),
    ("waiting_input", "claude_hook", False, False, False),
    ("idle", "claude_hook", False, False, False),
])
async def test_api_expires_working_hook_evidence_without_mutating_lifecycle(api, project,
                                                                        state, source, missing_times, fresh, stale):
    await pause_supervisor(api)
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=7)
    timestamp = now if fresh else old
    with write_db(api.app.state.db) as db:
        db.add(Session(id="stale-hook", project=db.get(Project, project["id"]), name="Demo admin",
                       provider="claude", tmux_name="acc-stale", run_as="agent", management="external",
                       desired_state="running", observed_state=state, status_source=source,
                       created_at=old, updated_at=now, status_at=None if missing_times else timestamp,
                       activity="Thinking", activity_at=None if missing_times else timestamp,
                       launch_spec=LaunchSpec(provider="claude", inputs={}, files={}, digest="test")))
        db.commit()
    detail = (await api.get("/api/sessions/stale-hook")).json()
    rows = (await api.get("/api/sessions")).json()
    summary = next(row for row in rows if row["id"] == "stale-hook")
    assert bool(detail.get("status_stale_since")) is stale
    assert summary.get("status_stale_since") == detail.get("status_stale_since")
    assert detail["observed_state"] == state
    if stale:
        assert datetime.fromisoformat(detail["status_stale_since"]) == old
        if not missing_times:
            with write_db(api.app.state.db) as db:
                db.get(Session, "stale-hook").created_at = now
                db.commit()
            assert (await api.get("/api/sessions/stale-hook")).json()["status_stale_since"] == detail["status_stale_since"]
            with write_db(api.app.state.db) as db:
                session = db.get(Session, "stale-hook")
                session.status_at = session.activity_at = now
                db.commit()
            assert (await api.get("/api/sessions/stale-hook")).json()["status_stale_since"] is None
    with api.app.state.db() as db:
        assert db.get(Session, "stale-hook").observed_state == state
