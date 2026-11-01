"""Supervisor behaviour against a real tmux server and the fake provider CLI (M2 acceptance)."""
from __future__ import annotations

import asyncio
import json
import sys
import threading

import pytest
from helpers import control, pause_supervisor, reconcile_passes, wait_for


async def launch(api, project, provider="claude", **extra):
    response = await api.post("/api/sessions", json={
        "project_id": project["id"], "provider": provider, "name": "work", **extra})
    assert response.status_code == 201, response.text
    return response.json()


async def get(api, acc_id):
    response = await api.get(f"/api/sessions/{acc_id}")
    assert response.status_code == 200, response.text
    return response.json()


def state_is(api, acc_id, observed, desired=None):
    async def check():
        session = await get(api, acc_id)
        ok = session["observed_state"] == observed and (desired is None or session["desired_state"] == desired)
        return session if ok else None
    return check


async def test_pausing_waits_for_already_running_thread_work(api):
    from acc.server import supervisor

    await pause_supervisor(api)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def worker():
        started.set()
        assert release.wait(15)
        finished.set()

    task = asyncio.create_task(supervisor.asyncio.to_thread(worker))
    api.supervisor_task = task
    await wait_for(started.is_set)
    cancelled = asyncio.get_running_loop().create_future()
    task.add_done_callback(lambda _: asyncio.get_running_loop().call_soon(cancelled.set_result, None))
    paused = asyncio.create_task(pause_supervisor(api))
    try:
        await cancelled
        assert task.cancelled() and not paused.done()
        release.set()
        await paused
        assert finished.is_set()
    finally:
        release.set()
        await paused

    def failed():
        raise ValueError("thread error consumed by its caller")

    with pytest.raises(ValueError, match="consumed by its caller"):
        await supervisor.asyncio.to_thread(failed)
    await pause_supervisor(api)  # do not report an already-handled stage failure again at shutdown


async def test_claude_launch_is_verified_by_session_start_hook(api, project, tmux_has):
    session = await launch(api, project)
    assert session["native_id"] and session["tmux_name"].startswith("acc-work-")
    ready = await wait_for(state_is(api, session["id"], "idle"))
    assert ready["status_source"] == "claude_hook"
    assert ready["runs"][0]["verified_at"] is not None
    assert tmux_has(session["tmux_name"])
    events = (await api.get(f"/api/sessions/{session['id']}/events")).json()
    starts = [e for e in events if e["kind"] == "SessionStart"]
    assert starts and starts[0]["payload"]["session_id"] == session["native_id"]


async def test_crash_resumes_same_conversation(api, project, env):
    session = await launch(api, project)
    await wait_for(state_is(api, session["id"], "idle"))
    control(env, session["id"], "crash")

    async def resumed():
        s = await get(api, session["id"])
        return s if len(s["runs"]) == 2 and s["runs"][1]["verified_at"] and s["observed_state"] == "idle" else None

    after = await wait_for(resumed)
    assert after["runs"][0]["exit_status"] == 3
    assert after["runs"][1]["reason"] == "resume"
    assert after["native_id"] == session["native_id"]
    assert after["recovery_attempts"] == 0  # reset once the resume was verified
    resume_events = [e for e in (await api.get(f"/api/sessions/{session['id']}/events")).json()
                     if e["kind"] == "SessionStart" and e["payload"].get("source") == "resume"]
    assert resume_events


async def test_clean_exit_is_not_resumed(api, project, env):
    session = await launch(api, project)
    await wait_for(state_is(api, session["id"], "idle"))
    control(env, session["id"], "exit")
    exited = await wait_for(state_is(api, session["id"], "exited", desired="stopped"))
    assert exited["runs"][0]["end_reason"] == "prompt_input_exit"
    await reconcile_passes(api)
    still = await get(api, session["id"])
    assert len(still["runs"]) == 1 and still["observed_state"] == "exited"


async def test_session_end_written_before_the_exit_is_kept_when_reconcile_sees_the_exit_first(api, project, env):
    """The CLI writes SessionEnd and then exits. When reconcile sees the dead pane before the ingest stage reads that
    line (other stages run between them, for seconds under load), the run must not close without its reason: it was
    the flake behind test_clean_exit_is_not_resumed. Here the ingest stage never runs after the session is idle."""
    session = await launch(api, project)
    await wait_for(state_is(api, session["id"], "idle"))
    supervisor = api.app.state.supervisor

    async def no_ingest():
        pass
    object.__setattr__(supervisor, "ingest_pass", no_ingest)
    control(env, session["id"], "exit")
    exited = await wait_for(state_is(api, session["id"], "exited", desired="stopped"))
    assert exited["runs"][0]["end_reason"] == "prompt_input_exit"


async def test_manual_resume_after_exit(api, project, env):
    session = await launch(api, project)
    await wait_for(state_is(api, session["id"], "idle"))
    control(env, session["id"], "exit")
    await wait_for(state_is(api, session["id"], "exited"))
    response = await api.post(f"/api/sessions/{session['id']}/resume")
    assert response.status_code == 200

    async def back():
        s = await get(api, session["id"])
        return s if len(s["runs"]) == 2 and s["observed_state"] == "idle" else None

    after = await wait_for(back)
    assert after["desired_state"] == "running" and after["runs"][1]["reason"] == "resume"


async def test_stop_is_respected(api, project, tmux_has):
    session = await launch(api, project)
    await wait_for(state_is(api, session["id"], "idle"))
    response = await api.post(f"/api/sessions/{session['id']}/stop")
    assert response.json()["observed_state"] == "stopped"
    assert not tmux_has(session["tmux_name"])
    await reconcile_passes(api)
    after = await get(api, session["id"])
    assert after["observed_state"] == "stopped" and len(after["runs"]) == 1
    assert not tmux_has(session["tmux_name"])


async def test_missing_transcript_needs_recovery_not_a_new_conversation(api, project, env):
    session = await launch(api, project)
    await wait_for(state_is(api, session["id"], "idle"))
    for path in (env["home"] / ".claude/projects").rglob(f"{session['native_id']}.jsonl"):
        path.unlink()
    control(env, session["id"], "crash")
    stuck = await wait_for(state_is(api, session["id"], "recovery_needed"))
    assert "wasn't found on the machine" in stuck["last_error"]
    assert len(stuck["runs"]) == 1  # nothing was launched in its place


async def test_failed_resumes_are_bounded(api, project, env):
    session = await launch(api, project)
    await wait_for(state_is(api, session["id"], "idle"))
    (env["run_dir"] / session["id"] / "fake.fail_resume").touch()
    control(env, session["id"], "crash")
    await wait_for(state_is(api, session["id"], "recovery_needed"), timeout=30)

    async def settled():  # the last attempt's exit status can land just after the state change
        current = await get(api, session["id"])
        return current if all(r["exit_status"] is not None for r in current["runs"][1:]) else None
    stuck = await wait_for(settled, timeout=10)
    assert len(stuck["runs"]) == 4  # launch + 3 resume attempts
    assert all(r["exit_status"] == 1 for r in stuck["runs"][1:])
    await reconcile_passes(api)
    assert len((await get(api, session["id"]))["runs"]) == 4


@pytest.mark.parametrize(("ending", "status", "exit_signal"), [
    ("raise SystemExit(1)", 1, None),
    ("os.kill(os.getpid(), signal.SIGTERM)", None, 15),
])
async def test_failed_resume_waits_for_exit_after_terminal_closes(api, project, env, ending, status, exit_signal):
    session = await launch(api, project)
    await wait_for(state_is(api, session["id"], "idle"))
    # A real tmux pane becomes dead on PTY EOF, before SIGCHLD supplies its exit result.
    # Stretch that interval deterministically instead of relying on poll timing at Python shutdown.
    failing_cli = env["home"] / "failing-cli"
    failing_cli.write_text(
        f"#!{sys.executable}\n"
        "import os, signal, time\n"
        "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
        "for fd in (0, 1, 2): os.close(fd)\n"
        "time.sleep(1)\n"
        f"{ending}\n"
    )
    failing_cli.chmod(0o755)
    env["broker"].providers["claude"] = str(failing_cli)
    control(env, session["id"], "crash")
    stuck = await wait_for(state_is(api, session["id"], "recovery_needed"))
    assert len(stuck["runs"]) == 4
    assert [(run["exit_status"], run["exit_signal"]) for run in stuck["runs"]] == [
        (3, None), (status, exit_signal), (status, exit_signal), (status, exit_signal)]
    events = (await api.get(f"/api/sessions/{session['id']}/events")).json()
    assert [(event["payload"]["exit_status"], event["payload"]["exit_signal"]) for event in events
            if event["kind"] == "process_exited"] == [
                (3, None), (status, exit_signal), (status, exit_signal), (status, exit_signal)]


async def test_permission_prompt_marks_waiting(api, project, env):
    session = await launch(api, project)
    await wait_for(state_is(api, session["id"], "idle"))
    control(env, session["id"], "permission")
    waiting = await wait_for(state_is(api, session["id"], "waiting_input"))
    assert waiting["status_source"] == "claude_hook"


async def test_codex_id_comes_from_hook(api, project, env):
    session = await launch(api, project, provider="codex")
    assert session["native_id"] is None
    ready = await wait_for(state_is(api, session["id"], "idle"))
    assert ready["native_id"]
    assert ready["status_source"] == "codex_hook"


async def test_untrusted_and_readonly_projects_refuse_launch(api, env):
    workspace = env["workspaces"] / "demo"
    untrusted = (await api.post("/api/projects", json={"slug": "u", "name": "U", "workspace_path": str(workspace)})).json()
    response = await api.post("/api/sessions", json={"project_id": untrusted["id"], "provider": "claude", "name": "x"})
    assert response.status_code == 409 and "trusted" in response.json()["detail"]
    readonly = (await api.post("/api/projects", json={"slug": "r", "name": "R", "workspace_path": str(workspace),
                                                       "import_mode": "readonly"})).json()
    await api.post(f"/api/projects/{readonly['id']}/trust")
    response = await api.post("/api/sessions", json={"project_id": readonly["id"], "provider": "claude", "name": "x"})
    assert response.status_code == 409 and "read-only" in response.json()["detail"]


async def test_trust_records_written_for_both_clis(api, project, env):
    claude = json.loads((env["home"] / ".claude.json").read_text())
    assert claude["projects"][project["workspace_path"]]["hasTrustDialogAccepted"] is True
    codex = (env["home"] / ".codex/config.toml").read_text()
    assert f'[projects."{project["workspace_path"]}"]' in codex and 'trust_level = "trusted"' in codex


async def test_api_requires_token(api):
    response = await api.get("/api/sessions", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401
    assert (await api.get("/api/health")).status_code == 200


async def test_supervisor_restart_adopts_live_session(api, project, env):
    from acc.server.app import create_app

    session = await launch(api, project)
    await wait_for(state_is(api, session["id"], "idle"))
    fresh = create_app(env["settings"])  # simulates accd restarting while the session runs
    await fresh.state.supervisor.reconcile()
    await fresh.state.supervisor.broker.close()
    await reconcile_passes(api)
    after = await get(api, session["id"])
    assert len(after["runs"]) == 1 and after["observed_state"] == "idle"


async def test_dangerous_launch_needs_explicit_confirmation(api, project):
    # This test isolates the generic confirmation after an owner's scoped
    # Security exception; confirmation itself must never create that exception.
    from acc.server import security_settings
    with api.app.state.db() as db:
        security_settings.save(db, {'kind': 'project', 'project_id': project['id']},
                               {'provider_prompts': False}, actor='casey', expected_revision=0)
        db.commit()
    body = {"project_id": project["id"], "provider": "claude", "name": "yolo", "permission_mode": "bypassPermissions"}
    refused = await api.post("/api/sessions", json=body)
    assert refused.status_code == 409
    accepted = await api.post("/api/sessions", json={**body, "confirm_dangerous": True})
    assert accepted.status_code == 201


async def test_external_session_is_observe_only(api, project, env):
    sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    response = await api.post("/api/sessions/external", json={
        "project_id": project["id"], "provider": "claude", "name": "root-admin", "native_id": sid,
        "run_as": "root", "unit": "acc-root-session.service"})
    assert response.status_code == 201, response.text
    session = response.json()
    run_dir = env["root_run_dir"] / session["id"]  # root's sessions: under the root-owned parent
    assert session["run_dir"] == str(run_dir)
    run_dir.mkdir(parents=True)

    def hook(event, **fields):
        record = {"ts": "2026-10-18T00:00:00Z", "provider": "claude", "acc_session": session["id"],
                  "hook": {"hook_event_name": event, "session_id": sid, **fields}}
        with (run_dir / "events.jsonl").open("a") as handle:
            handle.write(json.dumps(record) + "\n")

    hook("SessionStart", source="resume")
    (run_dir / "remote.json").write_text(json.dumps({"bridge_session_id": "session_01TEST"}))
    ready = await wait_for(state_is(api, session["id"], "idle"))
    assert ready["remote_url"] == "https://claude.ai/code/session_01TEST"
    assert ready["management"] == "external" and ready["run_as"] == "root"
    await reconcile_passes(api)
    assert (await get(api, session["id"]))["runs"] == []

    for verb in ("stop", "resume", "interrupt"):
        refused = await api.post(f"/api/sessions/{session['id']}/{verb}")
        assert refused.status_code == 409 and "Rynhult didn't start this session" in refused.json()["detail"]

    renamed = await api.post(f"/api/sessions/{session['id']}/rename", json={"name": "acc-host-root-admin"})
    assert renamed.status_code == 200 and renamed.json()["name"] == "acc-host-root-admin"
    assert renamed.json()["tmux_name"] == session["tmux_name"]  # display name only
    bad = await api.post(f"/api/sessions/{session['id']}/rename", json={"name": "$(rm -rf /)"})
    assert bad.status_code == 422

    hook("SessionEnd", reason="other")
    await wait_for(state_is(api, session["id"], "exited"))
    hook("SessionStart", source="resume")  # back after a reboot
    await wait_for(state_is(api, session["id"], "idle"))
    archived = await api.post(f"/api/sessions/{session['id']}/archive")
    assert archived.status_code == 200 and archived.json()["observed_state"] == "archived"


async def test_project_archive_refused_with_live_sessions(api, project):
    session = await launch(api, project)
    refused = await api.post(f"/api/projects/{project['id']}/archive")
    assert refused.status_code == 409
    await api.post(f"/api/sessions/{session['id']}/stop")
    archived = await api.post(f"/api/projects/{project['id']}/archive")
    assert archived.status_code == 200 and archived.json()["archived"] is True


async def test_launch_values_that_would_become_cli_options_are_refused(api, project):
    base = {"project_id": project["id"], "provider": "claude", "name": "opts"}
    for extra in ({"initial_prompt": "--dangerously-skip-permissions"}, {"initial_prompt": "-p hi"},
                  {"effort": 'high" --x'}, {"model": "--settings=/tmp/x"}, {"permission_mode": "--bypass"},
                  {"provider": "codex", "approval": "never --x"}, {"profile": "../x"}):
        refused = await api.post("/api/sessions", json={**base, **extra})
        assert refused.status_code == 422, (extra, refused.text)
    ok = await api.post("/api/sessions/preview", json={**base, "initial_prompt": "Fix -x handling", "model": "claude-opus-5-5[1m]", "effort": "high"})
    assert ok.status_code == 200, ok.text


async def test_sandbox_sent_inside_options_counts(api, project):
    """sandbox/approval/permission_mode are fields of their own: inside `options` they are moved there, so the
    command line uses them and the dangerous-launch confirmation still applies."""
    # This test isolates the generic confirmation after an owner's scoped
    # Security exception; confirmation itself must never create that exception.
    from acc.server import security_settings
    with api.app.state.db() as db:
        security_settings.save(db, {'kind': 'project', 'project_id': project['id']},
                               {'provider_prompts': False}, actor='casey', expected_revision=0)
        db.commit()
    body = {"project_id": project["id"], "provider": "codex", "name": "opts",
            "options": {"sandbox": "danger-full-access", "approval": "never"}}
    assert (await api.post("/api/sessions", json=body)).status_code == 409  # needs the confirmation
    preview = (await api.post("/api/sessions/preview", json=body)).json()
    command = " ".join(preview["command"])
    assert "--sandbox danger-full-access --ask-for-approval never" in command
    bad = await api.post("/api/sessions/preview", json={**body, "options": {"sandbox": "everything"}})
    assert bad.status_code == 422  # the field's own pattern still checks it


async def test_fast_session_start_is_not_consumed_before_launch_run_exists(api, project, env, monkeypatch):
    """A fast fake CLI can write its hook before the broker's launch reply reaches accd."""
    await pause_supervisor(api)
    supervisor = api.app.state.supervisor
    original = supervisor.broker.call

    async def delayed_reply(method, **params):
        result = await original(method, **params)
        if method == 'launch':
            spool = env['run_dir'] / params['acc_id'] / 'events.jsonl'
            await wait_for(lambda: spool.exists() and 'SessionStart' in spool.read_text())
            # Force the ingest/launch ordering that lost verification in the full browser run.
            supervisor.ingest_all()
        return result

    monkeypatch.setattr(supervisor.broker, 'call', delayed_reply)
    session = await launch(api, project)
    supervisor.ingest_all()
    ready = await get(api, session['id'])
    assert ready['runs'][0]['verified_at'] is not None
    assert ready['observed_state'] == 'idle'
    assert ready['last_error'] is None


async def test_remote_probe_does_not_pin_reconcile_read_snapshot(api, project, env, monkeypatch, caplog):
    from datetime import datetime, timezone

    from acc.server.models import Project, Session

    await pause_supervisor(api)
    supervisor = api.app.state.supervisor
    session = await launch(api, project, options={'remote_control': True})
    spool = env['run_dir'] / session['id'] / 'events.jsonl'
    await wait_for(lambda: spool.exists() and 'SessionStart' in spool.read_text())
    supervisor.ingest_all()
    original = supervisor.broker.call
    output_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    concurrent_commits = []

    async def interleaved(method, **params):
        if method == 'list':
            panes = await original(method, **params)
            for pane in panes:
                if pane['tmux_name'] == session['tmux_name']:
                    pane['last_output'] = output_at.timestamp()
            return panes
        if method == 'claude_remote':
            await asyncio.sleep(0)
            with supervisor.db() as other:
                other.get(Project, project['id']).name = 'Renamed during remote probe'
                other.commit()
            concurrent_commits.append(True)
            return {'bridge_session_id': 'session_01SNAPSHOT'}
        return await original(method, **params)

    monkeypatch.setattr(supervisor.broker, 'call', interleaved)
    await supervisor.reconcile()
    assert concurrent_commits == [True]
    with supervisor.db() as db:
        observed = db.get(Session, session['id'])
        assert observed.last_output_at == output_at
        assert observed.remote_url == 'https://claude.ai/code/session_01SNAPSHOT'
    assert not any('reconcile failed' in record.getMessage() for record in caplog.records)


async def test_maintenance_busy_skips_rest_of_pass_once(api, monkeypatch, caplog):
    import logging

    from acc.server.db import MaintenanceBusy
    await pause_supervisor(api)
    supervisor = api.app.state.supervisor
    seen = []
    async def busy():
        seen.append('machines')
        raise MaintenanceBusy('busy')
    async def later():
        seen.append('later')
    monkeypatch.setattr(supervisor, 'evaluate_machines', busy)
    monkeypatch.setattr(supervisor, 'autostart_projects', later)
    monkeypatch.setattr(supervisor.telemetry, 'tick', later)
    caplog.set_level(logging.INFO, 'acc.supervisor')
    object.__setattr__(supervisor.settings, 'ingest_interval', .05)
    supervisor._wake.set()
    started = asyncio.get_running_loop().time()
    task = asyncio.create_task(supervisor.run())
    try:
        await wait_for(lambda: seen.count('machines') >= 2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert not supervisor._wake.is_set()  # a stale wake must not spin through busy passes
    assert asyncio.get_running_loop().time() - started >= .05
    assert len(seen) >= 2 and set(seen) == {'machines'}
    records = [r for r in caplog.records if 'maintenance is busy' in r.message]
    assert len(records) == len(seen) and all(r.levelno == logging.INFO for r in records)
    assert not any(r.exc_info for r in caplog.records)


async def test_a_maintenance_lock_held_for_minutes_is_a_warning(api, monkeypatch, caplog):
    """Review 2026-10-25: skipped passes were INFO forever; a stuck lock stops ingest and reconcile silently."""
    import logging

    from acc.server import supervisor as module
    from acc.server.db import MaintenanceBusy
    await pause_supervisor(api)
    supervisor = api.app.state.supervisor
    passes = []
    async def busy():
        passes.append(1)
        raise MaintenanceBusy('busy')
    monkeypatch.setattr(supervisor, 'evaluate_machines', busy)
    monkeypatch.setattr(module, 'BUSY_WARN', .2)
    caplog.set_level(logging.INFO, 'acc.supervisor')
    object.__setattr__(supervisor.settings, 'ingest_interval', .02)
    task = asyncio.create_task(supervisor.run())
    try:
        await wait_for(lambda: any(r.levelno == logging.WARNING for r in caplog.records))
        warned = len(passes)
        await wait_for(lambda: len(passes) >= warned + 3)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and 'maintenance lock is still held' in warnings[0].message  # once per BUSY_WARN


@pytest.mark.parametrize('dead', [False, True])
async def test_start_and_exit_share_bounded_pending_hook_drain(api, project, env, monkeypatch, dead):
    from datetime import timedelta

    from acc.server import reconcile
    from acc.server.ingest import SPOOL_CHUNK
    from acc.server.models import AttentionItem, Session

    await pause_supervisor(api)
    supervisor = api.app.state.supervisor
    session = await launch(api, project)
    spool = env['run_dir'] / session['id'] / 'events.jsonl'
    await wait_for(lambda: spool.exists() and 'SessionStart' in spool.read_text())
    start_hook = spool.read_bytes()
    # Both lifecycle decisions must wait for a second bounded pass to reach the real hook.
    spool.write_bytes(b'\n' * (SPOOL_CHUNK + 1) + start_hook +
                      (json.dumps({'hook': {'hook_event_name': 'SessionEnd',
                                           'reason': 'prompt_input_exit'}}) + '\n').encode())
    after_timeout = reconcile.now() + timedelta(seconds=supervisor.settings.claude_verify_timeout + 1)
    monkeypatch.setattr(reconcile, 'now', lambda: after_timeout)
    pane = {'dead': dead, 'exit_status': 0 if dead else None, 'exit_signal': None}
    original = supervisor._ingest
    calls = []

    def counted(db, row):
        calls.append(row.id)
        return original(db, row)

    monkeypatch.setattr(supervisor, '_ingest', counted)
    with api.app.state.db() as db:
        row = db.get(Session, session['id'])
        await supervisor._reconcile_one(db, row, pane)
        assert calls == [row.id]
        assert row.spool_offset == SPOOL_CHUNK
        assert row.runs[-1].verified_at is None
        assert row.runs[-1].ended_at is None
        assert row.last_error is None
        assert db.query(AttentionItem).filter_by(dedupe_key=f'needs_input:{row.id}').first() is None
        db.commit()
    with api.app.state.db() as db:
        row = db.get(Session, session['id'])
        await supervisor._reconcile_one(db, row, pane)
        assert calls == [row.id, row.id]
        assert row.runs[-1].verified_at is not None
        assert row.runs[-1].end_reason == 'prompt_input_exit'
        assert (row.runs[-1].ended_at is not None) == dead
        assert row.observed_state == ('exited' if dead else 'idle')
        assert row.last_error is None
        db.commit()


async def test_exit_waits_for_bounded_hook_backlog_and_keeps_reason(api, project, env):
    from sqlalchemy import select

    from acc.server.ingest import SPOOL_CHUNK
    from acc.server.models import Event, Session

    session = await launch(api, project)
    await wait_for(state_is(api, session['id'], 'idle'))
    await pause_supervisor(api)
    supervisor = api.app.state.supervisor
    spool = env['run_dir'] / session['id'] / 'events.jsonl'
    with spool.open('ab') as stream:
        stream.write(b'\n' * (SPOOL_CHUNK + 1))
        stream.write((json.dumps({'hook': {'hook_event_name': 'SessionEnd', 'reason': 'prompt_input_exit'}}) + '\n').encode())
    pane = {'dead': True, 'exit_status': 0, 'exit_signal': None}
    with api.app.state.db() as db:
        row = db.get(Session, session['id'])
        before = row.spool_offset
        await supervisor._reconcile_one(db, row, pane)
        assert row.spool_offset - before <= SPOOL_CHUNK
        assert row.runs[-1].ended_at is None
        db.commit()
    with api.app.state.db() as db:
        row = db.get(Session, session['id'])
        await supervisor._reconcile_one(db, row, pane)
        db.commit()
    supervisor.ingest_all()
    with api.app.state.db() as db:
        row = db.get(Session, session['id'])
        assert row.runs[-1].end_reason == 'prompt_input_exit'
        assert len(list(db.scalars(select(Event).where(Event.session_id == row.id, Event.kind == 'SessionEnd')))) == 1


async def test_rolled_back_hook_requests_are_not_dispatched_or_duplicated(api, project, env):
    from acc.server.models import Session

    session = await launch(api, project)
    await wait_for(state_is(api, session['id'], 'idle'))
    await pause_supervisor(api)
    supervisor = api.app.state.supervisor
    spool = env['run_dir'] / session['id'] / 'events.jsonl'
    with spool.open('a') as stream:
        stream.write(json.dumps({'hook': {'hook_event_name': 'AccRequest', 'request': {
            'type': 'publish', 'host': 'demo.example.com', 'port': 8100}}}) + '\n')
    with api.app.state.db() as db:
        supervisor._ingest(db, db.get(Session, session['id']))
        db.rollback()
    assert supervisor._deferred == []
    with api.app.state.db() as db:
        supervisor._ingest(db, db.get(Session, session['id']))
        db.commit()
    assert len(supervisor._deferred) == 1
    with api.app.state.db() as db:
        supervisor._ingest(db, db.get(Session, session['id']))
        db.commit()
    assert len(supervisor._deferred) == 1


@pytest.mark.parametrize('unavailable_first', [False, True])
async def test_remote_exit_refreshes_bounded_spool_without_a_read_transaction(api, project, monkeypatch, unavailable_first):
    import base64

    from acc.server import reconcile as module
    from acc.server.db import _transaction_gate
    from acc.server.models import Session
    from acc.server.supervisor import REMOTE_SPOOL_CHUNK

    session = await launch(api, project)
    await wait_for(state_is(api, session['id'], 'idle'))
    await pause_supervisor(api)
    supervisor = api.app.state.supervisor
    with api.app.state.db() as db:
        row = db.get(Session, session['id'])
        row.placement.host = 'remote-example'
        row.spool_offset = 0
        db.commit()
    data = b'\n' * REMOTE_SPOOL_CHUNK + (json.dumps({'hook': {
        'hook_event_name': 'SessionEnd', 'reason': 'prompt_input_exit'}}) + '\n').encode()
    calls = []
    original = supervisor.broker.call
    gate = _transaction_gate(api.app.state.db.kw['bind'])
    async def broker(method, **params):
        if params.get('host') != 'remote-example':
            return await original(method, **params)
        if method == 'list':
            return [{'tmux_name': session['tmux_name'], 'dead': True, 'exit_status': 0, 'exit_signal': None}]
        assert method == 'read_spool'
        assert gate.readers == 0 and not gate.worker
        calls.append(params['offset'])
        if unavailable_first and len(calls) == 1:
            from acc.server.broker_client import BrokerUnavailable
            raise BrokerUnavailable('temporary test disconnect')
        return {'data': base64.b64encode(data[params['offset']:params['offset'] + REMOTE_SPOOL_CHUNK]).decode()}
    monkeypatch.setattr(supervisor.broker, 'call', broker)
    monkeypatch.setattr(module, 'connected', lambda *_: True)
    if unavailable_first:
        await supervisor.reconcile()
        with api.app.state.db() as db:
            assert db.get(Session, session['id']).runs[-1].ended_at is None
    await supervisor.reconcile()
    with api.app.state.db() as db:
        assert db.get(Session, session['id']).runs[-1].ended_at is None
    await supervisor.reconcile()
    with api.app.state.db() as db:
        assert db.get(Session, session['id']).runs[-1].end_reason == 'prompt_input_exit'
    assert calls == ([0] if unavailable_first else []) + [0, REMOTE_SPOOL_CHUNK]


async def test_failed_hook_savepoint_does_not_queue_its_proposal(api, project, env, monkeypatch):
    from acc.server.models import Session
    session = await launch(api, project)
    await wait_for(state_is(api, session['id'], 'idle'))
    await pause_supervisor(api)
    supervisor = api.app.state.supervisor
    original = supervisor._apply_hook
    def fault(db, row, record):
        original(db, row, record)
        if record.get('fail'):
            raise ValueError('injected hook failure')
    monkeypatch.setattr(supervisor, '_apply_hook', fault)
    spool = env['run_dir'] / session['id'] / 'events.jsonl'
    with spool.open('a') as stream:
        for failed in (True, False):
            stream.write(json.dumps({'fail': failed, 'hook': {'hook_event_name': 'AccRequest', 'request': {
                'type': 'publish', 'host': 'bad.example.com' if failed else 'good.example.com', 'port': 8100}}}) + '\n')
    with api.app.state.db() as db:
        supervisor._ingest(db, db.get(Session, session['id']))
        db.commit()
    assert [request[-1]['host'] for request in supervisor._deferred] == ['good.example.com']


async def test_dead_run_with_a_partial_final_hook_does_not_wait_forever(api, project, env):
    from acc.server.models import Session
    session = await launch(api, project)
    await wait_for(state_is(api, session['id'], 'idle'))
    await pause_supervisor(api)
    with (env['run_dir'] / session['id'] / 'events.jsonl').open('ab') as stream:
        stream.write(b'{')
    with api.app.state.db() as db:
        row = db.get(Session, session['id'])
        await api.app.state.supervisor._reconcile_one(db, row, {'dead': True, 'exit_status': 0, 'exit_signal': None})
        db.commit()
        assert row.runs[-1].ended_at is not None


async def test_launch_failure_event_and_status_hide_broker_exception_names(api, project, monkeypatch):
    from acc.server.broker_client import BrokerError
    original = api.app.state.supervisor.broker.call

    async def refuse(method, **kwargs):
        if method == 'launch':
            raise BrokerError("OpError: the session's terminal didn't respond (tmux new-session): command too long")
        return await original(method, **kwargs)

    monkeypatch.setattr(api.app.state.supervisor.broker, 'call', refuse)
    session = await launch(api, project)

    async def failed():
        events = (await api.get(f"/api/sessions/{session['id']}/events")).json()
        return next((event for event in events if event['kind'] == 'launch_failed'), None)

    event = await wait_for(failed)
    assert event['payload']['error'] == "the session's terminal didn't respond (tmux new-session): command too long"
    current = await get(api, session['id'])
    assert 'OpError:' not in current['last_error']
