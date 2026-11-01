"""Idle sessions cost the background loops nothing (rc performance check: ~80% of a core with 1,333 idle sessions).
Ingest skips a session whose files haven't changed since a pass read all of them; reconcile skips a stopped session
whose pane is gone. Anything new or active goes through exactly as before."""
from __future__ import annotations

import pytest
from helpers import pause_supervisor, reconcile_passes, wait_for
from sqlalchemy import func, select

from acc.server import ingest, reconcile
from acc.server.models import Event, Session


async def launched(api, project, env):
    response = await api.post("/api/sessions", json={"project_id": project["id"], "provider": "claude", "name": "work"})
    assert response.status_code == 201, response.text
    session = response.json()
    spool = env["run_dir"] / session["id"] / "events.jsonl"
    await wait_for(lambda: spool.exists() and "SessionStart" in spool.read_text())
    return session["id"], spool


@pytest.fixture
def ingested(api, monkeypatch):
    """Which sessions each ingest pass actually opened."""
    supervisor = api.app.state.supervisor
    calls = []
    original = supervisor._ingest

    def counting(db, session):
        calls.append(session.id)
        return original(db, session)

    monkeypatch.setattr(supervisor, "_ingest", counting)
    return supervisor, calls


def events(api, acc_id):
    with api.app.state.db() as db:
        return db.scalar(select(func.count()).select_from(Event).where(Event.session_id == acc_id))


async def settle(supervisor, calls):
    for _ in range(3):  # the pass that reads new lines, then one that sees the new offset
        calls.clear()
        supervisor.ingest_all()
        if not calls:
            return
    raise AssertionError(f"still reading {calls} after three passes")


async def test_an_unchanged_session_is_skipped_and_a_new_line_is_read(api, project, env, ingested):
    await pause_supervisor(api)
    supervisor, calls = ingested
    acc_id, spool = await launched(api, project, env)
    await settle(supervisor, calls)
    before = events(api, acc_id)
    supervisor.ingest_all()
    assert calls == []  # idle: no transaction, no read
    with spool.open("a") as handle:
        handle.write(spool.read_text().splitlines()[0] + "\n")  # one more hook line
    supervisor.ingest_all()
    assert calls == [acc_id] and events(api, acc_id) == before + 1


async def test_a_backlog_longer_than_one_pass_keeps_being_read(api, project, env, ingested, monkeypatch):
    await pause_supervisor(api)
    supervisor, calls = ingested
    acc_id, spool = await launched(api, project, env)
    await settle(supervisor, calls)
    line = spool.read_text().splitlines()[0] + "\n"
    monkeypatch.setattr(ingest, "SPOOL_CHUNK", len(line) * 2)  # two lines per pass
    with spool.open("a") as handle:
        handle.write(line * 7)
    size = spool.stat().st_size
    for _ in range(8):
        supervisor.ingest_all()
    with api.app.state.db() as db:
        assert db.get(Session, acc_id).spool_offset == size  # nothing settled while lines were left


async def test_a_full_pass_reads_every_session_again(api, project, env, ingested):
    await pause_supervisor(api)
    supervisor, calls = ingested
    acc_id, _ = await launched(api, project, env)
    await settle(supervisor, calls)
    supervisor._ingest_full_at = float("-inf")  # FULL_PASS_SECONDS have passed
    supervisor.ingest_all()
    assert calls == [acc_id]
    calls.clear()
    supervisor.ingest_all()
    assert calls == []


async def test_a_reset_offset_or_a_launching_session_is_never_settled_wrongly(api, project, env, ingested):
    await pause_supervisor(api)
    supervisor, calls = ingested
    acc_id, _ = await launched(api, project, env)
    await settle(supervisor, calls)
    with api.app.state.db() as db:  # e.g. a restore resets the offset: the session's mark changes
        db.get(Session, acc_id).spool_offset = 0
        db.commit()
    supervisor.ingest_all()
    assert calls == [acc_id]
    await settle(supervisor, calls)
    supervisor._launching.add(acc_id)
    try:
        supervisor._ingest_full_at = float("-inf")
        supervisor.ingest_all()
        assert calls == [] and acc_id not in supervisor._ingest_seen  # skipped, not marked as read
    finally:
        supervisor._launching.discard(acc_id)


async def test_marks_find_the_same_spool_as_the_ingest_itself(api, project, env):
    await pause_supervisor(api)
    supervisor = api.app.state.supervisor
    acc_id, spool = await launched(api, project, env)
    marks = supervisor.ingest_marks()
    with api.app.state.db() as db:
        session = db.get(Session, acc_id)
        assert ingest.spool_path(supervisor.settings, session) == spool
    assert marks[acc_id][2] == ingest._file_mark(spool)


@pytest.fixture
def reconciled(api, monkeypatch):
    """Which sessions each reconcile pass actually opened."""
    supervisor = api.app.state.supervisor
    calls = []
    original = supervisor._reconcile_one

    async def counting(db, session, pane, **kwargs):  # gate-nb adds spool_pending=
        calls.append(session.id)
        return await original(db, session, pane, **kwargs)

    monkeypatch.setattr(supervisor, "_reconcile_one", counting)
    return calls


async def test_reconcile_skips_a_stopped_session_whose_pane_is_gone(api, project, env, reconciled):
    await pause_supervisor(api)
    acc_id, _ = await launched(api, project, env)
    await reconcile_passes(api, 1)
    assert acc_id in reconciled  # running: reconciled every pass
    assert (await api.post(f"/api/sessions/{acc_id}/stop")).status_code == 200

    async def stopped():
        await reconcile_passes(api, 1)
        with api.app.state.db() as db:
            return db.get(Session, acc_id).observed_state == "stopped"
    await wait_for(stopped)
    reconciled.clear()
    await reconcile_passes(api, 2)
    assert acc_id not in reconciled  # nothing left to do: no transaction
    assert (await api.post(f"/api/sessions/{acc_id}/resume")).status_code == 200
    await reconcile_passes(api, 1)
    assert acc_id in reconciled  # active again: reconciled as before


@pytest.mark.parametrize("change", [
    {"desired_state": "running"}, {"observed_state": "exited"}, {"container": True}, {"team_stop_pending": True},
    {"multirun_stop_pending": True}, {"idle_since": "now"},
    {"multirun_id": "m1", "last_error": "stop not confirmed: still running"},
    {"last_error": "stop not confirmed: still running"},
])
def test_only_a_settled_stop_is_skipped(change):
    import datetime as dt
    from types import SimpleNamespace
    base = dict(desired_state="stopped", observed_state="stopped", container=False, team_stop_pending=False,
                multirun_stop_pending=False, idle_since=None, multirun_id=None, sleep_paused_at=None, last_error=None,
                project=SimpleNamespace(group_id=None), launch_spec=SimpleNamespace(files={}))
    assert reconcile.settled_stop(SimpleNamespace(**base))
    value = {k: (dt.datetime.now(dt.timezone.utc) if v == "now" else v) for k, v in change.items()}
    assert not reconcile.settled_stop(SimpleNamespace(**{**base, **value}))
    assert not reconcile.settled_stop(SimpleNamespace(**{**base, "project": SimpleNamespace(group_id="g1")}))
    assert not reconcile.settled_stop(SimpleNamespace(**{**base, "launch_spec": SimpleNamespace(files={"explanation.json": "x"})}))


# ---- independent review (rc-performance-review) -------------------------------------------------------------------

async def test_a_file_replaced_with_the_same_size_and_time_is_still_read(api, project, env, ingested):
    """A rename over the spool, statusline or remote.json can keep the size and land in the same timestamp tick (or a
    writer can copy the old time): the mark also names the file itself, so its new content is read now, not in 10 min."""
    import os
    await pause_supervisor(api)
    supervisor, calls = ingested
    acc_id, spool = await launched(api, project, env)
    await settle(supervisor, calls)
    old = os.lstat(spool)
    replacement = spool.with_name("events.jsonl.new")
    replacement.write_bytes(spool.read_bytes())  # same size
    os.utime(replacement, ns=(old.st_atime_ns, old.st_mtime_ns))  # and the same modification time
    os.replace(replacement, spool)
    assert (os.lstat(spool).st_size, os.lstat(spool).st_mtime_ns) == (old.st_size, old.st_mtime_ns)
    supervisor.ingest_all()
    assert calls == [acc_id]


async def test_a_hook_appended_within_the_same_tick_is_read(api, project, env, ingested):
    import os
    await pause_supervisor(api)
    supervisor, calls = ingested
    acc_id, spool = await launched(api, project, env)
    await settle(supervisor, calls)
    old = os.lstat(spool)
    before = events(api, acc_id)
    with spool.open("a") as handle:
        handle.write(spool.read_text().splitlines()[0] + "\n")
    os.utime(spool, ns=(old.st_atime_ns, old.st_mtime_ns))  # as if appended within the same tick
    supervisor.ingest_all()
    assert calls == [acc_id] and events(api, acc_id) == before + 1


async def test_a_stop_resumed_while_the_pass_lists_panes_is_reconciled_in_that_pass(api, project, env, reconciled):
    """The settled set comes from a snapshot taken before the broker calls; a resume made meanwhile must not be skipped."""
    from helpers import write_db
    await pause_supervisor(api)
    acc_id, _ = await launched(api, project, env)
    assert (await api.post(f"/api/sessions/{acc_id}/stop")).status_code == 200

    async def stopped():
        await reconcile_passes(api, 1)
        with api.app.state.db() as db:
            return db.get(Session, acc_id).observed_state == "stopped"
    await wait_for(stopped)
    supervisor = api.app.state.supervisor
    original = supervisor.broker.call

    async def listing(method, **params):
        result = await original(method, **params)
        if method == "list":  # a person resumes it while the pass is still listing panes
            with write_db(api.app.state.db) as db:
                db.get(Session, acc_id).desired_state = "running"
                db.commit()
        return result
    supervisor.broker.call = listing
    try:
        reconciled.clear()
        await supervisor.reconcile()
    finally:
        supervisor.broker.call = original
    assert acc_id in reconciled


async def test_a_settled_stop_skipped_by_reconcile_still_has_its_late_hooks_read(api, project, env, reconciled, ingested):
    """gate-nb2: reconcile's settled skip and its bounded hook drain (pending hooks before closure or the startup
    timeout, for sessions that should run) never overlap, and a late hook of a settled stop is read by ingest."""
    await pause_supervisor(api)
    supervisor, calls = ingested
    acc_id, spool = await launched(api, project, env)
    assert (await api.post(f"/api/sessions/{acc_id}/stop")).status_code == 200

    async def stopped():
        await reconcile_passes(api, 1)
        with api.app.state.db() as db:
            return db.get(Session, acc_id).observed_state == "stopped"
    await wait_for(stopped)
    await settle(supervisor, calls)
    before = events(api, acc_id)
    with spool.open("a") as handle:  # e.g. its SessionEnd, written as the process went away
        handle.write(spool.read_text().splitlines()[0] + "\n")
    reconciled.clear()
    await reconcile_passes(api, 1)
    assert acc_id not in reconciled  # settled: no reconcile transaction
    supervisor.ingest_all()
    assert acc_id in calls and events(api, acc_id) == before + 1  # but ingest reads the new hook
