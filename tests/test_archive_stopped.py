"""Bulk archival preserves stopped session resources and clears old activity."""
import pytest
from helpers import pause_supervisor, write_db

from acc.server.api.context import Principal
from acc.server.models import LaunchSpec, Project, Session


@pytest.mark.parametrize("owner,expected", [(True, 2), (False, 1)])
async def test_archive_stopped_scope_and_preservation(api, project, owner, expected):
    await pause_supervisor(api)
    with write_db(api.app.state.db) as db:
        for sid, person, desired, observed in [
            ("mine", "alice", "stopped", "stopped"),
            ("theirs", "bob", "stopped", "stopped"),
            ("live", "alice", "running", "idle"),
            ("pending", "alice", "stopped", "active"),
            ("old", "alice", "archived", "archived"),
        ]:
            db.add(Session(id=sid, project=db.get(Project, project["id"]), owner=person, name=sid,
                           provider="claude", tmux_name=f"acc-{sid}", run_as="agent",
                           desired_state=desired, observed_state=observed, activity="Thinking",
                           worktree=f"/tmp/example/{sid}", worktree_branch=f"task/{sid}",
                           launch_spec=LaunchSpec(provider="claude", inputs={}, files={}, digest="test")))
        db.commit()
    # Override the existing auth dependency, retaining the real session authorization checks.
    dependency = api.app.state.ctx.principal
    api.app.dependency_overrides[dependency] = lambda: Principal("alice", "owner" if owner else "member", "agent")
    try:
        response = await api.post("/api/sessions/archive-stopped")
        assert response.status_code == 200, response.text
        assert response.json() == {"archived": expected, "more_remain": False}
        assert (await api.post("/api/sessions/archive-stopped")).json() == {"archived": 0, "more_remain": False}
        with api.app.state.db() as db:
            mine = db.get(Session, "mine")
            assert mine.desired_state == mine.observed_state == "archived"
            assert mine.worktree == "/tmp/example/mine" and mine.worktree_branch == "task/mine"
            assert db.get(Session, "theirs").desired_state == ("archived" if owner else "stopped")
            assert db.get(Session, "live").desired_state == "running"
            assert db.get(Session, "pending").observed_state == "active"
        stopped = (await api.get("/api/sessions/pending")).json()
        assert stopped["activity"] is None
    finally:
        api.app.dependency_overrides.clear()


async def test_archive_stopped_refuses_installation_token(api):
    response = await api.post("/api/sessions/archive-stopped")
    assert response.status_code == 403
    assert "signed-in person" in response.json()["detail"]


async def test_archive_stopped_batch_cap(api, project):
    await pause_supervisor(api)
    with write_db(api.app.state.db) as db:
        for index in range(202):
            db.add(Session(id=f"batch-{index:03}", project=db.get(Project, project["id"]),
                           owner="alice", name="Stopped", provider="claude", tmux_name=f"batch-{index}",
                           run_as="agent", desired_state="stopped", observed_state="stopped",
                           launch_spec=LaunchSpec(provider="claude", inputs={}, files={}, digest="test")))
        db.commit()
    api.app.dependency_overrides[api.app.state.ctx.principal] = lambda: Principal("alice", "member", "agent")
    try:
        response = await api.post("/api/sessions/archive-stopped")
        assert response.status_code == 200, response.text
        assert response.json() == {"archived": 200, "more_remain": True}
        assert (await api.post("/api/sessions/archive-stopped")).json() == {"archived": 2, "more_remain": False}
    finally:
        api.app.dependency_overrides.clear()


async def test_archive_stopped_rechecks_state_under_each_lock(api, project, monkeypatch):
    await pause_supervisor(api)
    with write_db(api.app.state.db) as db:
        for sid in ("race-a", "race-b"):
            db.add(Session(id=sid, project=db.get(Project, project["id"]), owner="alice", name=sid,
                           provider="claude", tmux_name=sid, run_as="agent",
                           desired_state="stopped", observed_state="stopped",
                           launch_spec=LaunchSpec(provider="claude", inputs={}, files={}, digest="test")))
        db.commit()
    supervisor = api.app.state.ctx.supervisor
    original_lock = supervisor._lock

    class RestartOnLock:
        acquisitions = 0

        async def __aenter__(self):
            await original_lock.acquire()
            self.acquisitions += 1
            if self.acquisitions == 1:
                with write_db(api.app.state.db) as db:
                    db.get(Session, "race-a").desired_state = "running"
                    db.commit()

        async def __aexit__(self, *args):
            original_lock.release()

    lock = RestartOnLock()
    monkeypatch.setattr(supervisor, "_lock", lock)
    api.app.dependency_overrides[api.app.state.ctx.principal] = lambda: Principal("alice", "member", "agent")
    try:
        response = await api.post("/api/sessions/archive-stopped")
        assert response.json() == {"archived": 1, "more_remain": False}
        assert lock.acquisitions == 2
        with api.app.state.db() as db:
            assert db.get(Session, "race-a").desired_state == "running"
            assert db.get(Session, "race-b").desired_state == "archived"
    finally:
        api.app.dependency_overrides.clear()
