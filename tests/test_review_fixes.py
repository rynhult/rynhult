"""Fixes from the overnight review (docs/reviews/review-codex.md): each test is the scenario the review described."""
from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest
from helpers import pause_supervisor, wait_for, write_db
from isolated_settings import isolated_broker
from test_changes import git, repo
from test_supervisor import get, launch, state_is

from acc.broker import ops
from acc.server.broker_client import BrokerError
from acc.server.db import WRITE_INTENT
from acc.server.models import Observation, Session


def config(tmp_path):
    return isolated_broker(tmp_path, socket_path=tmp_path / "s", run_dir=tmp_path, tmux_socket="x", allowed_roots=(tmp_path,),
                        allowed_uids=frozenset(), providers={}, hook_path="x")


def test_worktree_with_ignored_files_is_kept(tmp_path):
    repo(tmp_path / "proj")
    (tmp_path / "proj" / ".gitignore").write_text("private/\n")
    git(tmp_path / "proj", "add", ".gitignore")
    git(tmp_path / "proj", "commit", "-qm", "ignore")
    made = ops.make_worktree(config(tmp_path), str(tmp_path / "proj"), str(tmp_path), "proj-1", "acc/x-1")
    (Path(made["path"]) / "private").mkdir()
    (Path(made["path"]) / "private" / "notes.txt").write_text("only copy")
    kept = ops.remove_worktree(config(tmp_path), str(tmp_path / "proj"), made["path"])
    assert kept["removed"] is False and "ignores" in kept["reason"]
    assert (Path(made["path"]) / "private" / "notes.txt").read_text() == "only copy"


def test_failed_git_reads_are_errors_not_zero_changes(tmp_path, monkeypatch):
    repo(tmp_path / "ws")
    (tmp_path / "ws" / "app.py").write_text("changed\n")
    real = ops._git

    def broken(cwd, *args, **kw):
        if args[0] == "diff" and "--numstat" in args:
            return subprocess.CompletedProcess(args, 128, "", "fatal: bad object")
        return real(cwd, *args, **kw)
    monkeypatch.setattr(ops, "_git", broken)
    with pytest.raises(ops.OpError, match="bad object"):
        ops.workspace_changes(config(tmp_path), str(tmp_path / "ws"), None)


def test_failed_screen_capture_is_unknown_not_no_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "tmux", lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "no server"))
    with pytest.raises(ops.OpError):
        ops.screen_prompt(config(tmp_path), "acc-x-12345678", "cursor")


def test_sqlite_wal_changes_change_the_stamp(tmp_path, monkeypatch):
    chat = tmp_path / ".cursor" / "chats" / "h" / "11111111-2222-3333-4444-555555555555"
    chat.mkdir(parents=True)
    db = sqlite3.connect(chat / "store.db")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
    db.commit()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    first = ops.transcript_messages("cursor", "11111111-2222-3333-4444-555555555555", None)
    db.execute("INSERT INTO blobs VALUES ('1', ?)", (b'{"role":"user","content":"hello"}',))
    db.commit()  # lands in store.db-wal; store.db's size and mtime don't change
    second = ops.transcript_messages("cursor", "11111111-2222-3333-4444-555555555555", first["stamp"])
    assert not second.get("unchanged") and [m["text"] for m in second["messages"]] == ["hello"]
    db.close()


async def test_extra_five_percent_is_saved(api):
    intent = WRITE_INTENT.set(True)
    with write_db(api.app.state.db) as s:
        s.merge(Observation(key="claude.rate_limits", source="t", observed_at=0, value={
            "seven_day": {"used_percentage": 85, "resets_at": 4_000_000_000}}))
        s.commit()
    WRITE_INTENT.reset(intent)
    await api.put("/api/budgets", json={"rules": {"claude.seven_day": {"mode": "cap", "limit": 80}}})
    await api.post("/api/budgets/extra", json={"window": "claude.seven_day"})
    await api.post("/api/budgets/extra", json={"window": "claude.seven_day"})
    fresh = (await api.get("/api/budgets")).json()  # a new request: read back from the database
    assert fresh["rules"]["claude.seven_day"]["extra"] == 10 and fresh["status"]["claude.seven_day"]["allowed"] == 90


async def test_unreadable_transcript_is_retried_not_marked_indexed(api, project, env, monkeypatch):
    session = await launch(api, project)
    await wait_for(state_is(api, session["id"], "idle"))
    # This test drives the history stage itself. The loop's own pass may be mid-read (it can read and index the
    # transcript after the reset below, correctly so): stop the loop and wait for its work first.
    await pause_supervisor(api)
    supervisor = api.app.state.supervisor
    real = supervisor.broker.call

    async def failing(method, *a, **kw):
        if method == "transcript_messages":
            raise BrokerError("permission denied")
        return await real(method, *a, **kw)
    monkeypatch.setattr(supervisor.broker, "call", failing)
    intent = WRITE_INTENT.set(True)
    with write_db(api.app.state.db) as s:  # the loop may have indexed it already: start from "never indexed"
        s.get(Session, session["id"]).indexed_at = None
        s.commit()
    WRITE_INTENT.reset(intent)
    supervisor.__dict__.get("_history_retry", {}).pop(session["id"], None)
    await supervisor.index_transcripts()
    with api.app.state.db() as s:
        assert s.get(Session, session["id"]).indexed_at is None  # not read, so not "indexed"
    assert session["id"] in supervisor._history_retry  # tried again later, with backoff


async def test_stop_records_the_intent_even_if_the_kill_fails(api, project, env, monkeypatch):
    session = await launch(api, project)
    await wait_for(state_is(api, session["id"], "idle"))
    supervisor = api.app.state.supervisor
    real = supervisor.broker.call
    from acc.server.broker_client import BrokerError

    async def kill_fails(method, *a, **kw):
        if method == "kill":
            raise BrokerError("broker went away")
        return await real(method, *a, **kw)
    monkeypatch.setattr(supervisor.broker, "call", kill_fails)
    await api.post(f"/api/sessions/{session['id']}/archive")
    s = await get(api, session["id"])
    assert s["desired_state"] == "archived"  # recorded first; reconcile finishes the kill


async def test_launch_failure_after_recording_keeps_the_worktree(api, project, env, monkeypatch):
    repo(env["workspaces"] / "demo")
    supervisor = api.app.state.supervisor

    async def boom(*a, **kw):
        raise RuntimeError("database busy after the session was recorded")
    monkeypatch.setattr(supervisor, "_start", boom)
    response = await api.post("/api/sessions", json={"project_id": project["id"], "provider": "claude", "name": "wt",
                                                     "worktree": True})
    assert response.status_code == 201
    assert Path(response.json()["worktree"]).exists()  # the recorded session still owns it


def test_spool_reads_never_follow_a_linked_session_folder(tmp_path, monkeypatch):
    """Security review #2: an agent replaces its session folder with a link into another account's run folder."""
    from acc.server import ingest
    root = tmp_path / "run"
    (root / "victim" / "s2").mkdir(parents=True)
    (root / "victim" / "s2" / "events.jsonl").write_text('{"secret": true}\n')
    (root / "attacker").mkdir()
    (root / "attacker" / "s1").symlink_to(root / "victim" / "s2")
    monkeypatch.setattr(ingest, "TRUSTED_ROOTS", (root,))
    assert ingest.read_spool_file(root / "attacker" / "s1" / "events.jsonl") is None
    (root / "attacker" / "s3").mkdir()
    (root / "attacker" / "s3" / "events.jsonl").write_text("ok\n")
    assert ingest.read_spool_file(root / "attacker" / "s3" / "events.jsonl") == b"ok\n"  # plain folders still work


async def test_members_projects_never_use_the_owners_github_token(api, env):
    """Security review #1: a member's project naming a private repo must not be read with the owner's token."""
    from acc.server.models import Project, User
    supervisor = api.app.state.supervisor
    seen = []

    class FakeGitHub:
        async def snapshot(self, repo):
            seen.append(repo)
            return {"fetched_at": 0, "repo": {"default_branch": "main"}, "runs": [], "pulls": []}
    supervisor._github = FakeGitHub()
    intent = WRITE_INTENT.set(True)
    with write_db(api.app.state.db) as s:
        s.add(User(username="mem", role="member", password_hash="x", totp_secret="x"))
        s.add(Project(account="agent", slug="mine", name="Mine", workspace_path=str(env["workspaces"] / "a"), repo="org/owner-repo", owner="@api"))
        s.add(Project(account="agent", slug="theirs", name="Theirs", workspace_path=str(env["workspaces"] / "b"), repo="org/private", owner="mem"))
        s.commit()
    WRITE_INTENT.reset(intent)
    await supervisor.refresh_github()
    assert "org/private" not in seen and "org/owner-repo" in seen  # the owner token's ("@api") still is
    assert (await api.post("/api/projects", json={"slug": "bad", "name": "b", "workspace_path": "/x",
                                                  "repo": "../../etc"})).status_code == 422


async def test_a_session_that_never_reports_starting_needs_you(api, project, env, monkeypatch):
    """Overnight 2026-10-22: Claude sat at its own bypass dialog for hours while ACC showed "working"."""
    supervisor = api.app.state.supervisor
    object.__setattr__(supervisor.settings, "claude_verify_timeout", 1.0)
    real = supervisor._apply_hook

    def no_start(db, session, record):
        if (record.get("hook") or {}).get("hook_event_name") == "SessionStart":
            return  # as if the CLI were stuck before it could report
        return real(db, session, record)
    monkeypatch.setattr(supervisor, "_apply_hook", no_start)
    session = await launch(api, project)
    stuck = await wait_for(state_is(api, session["id"], "waiting_input"), timeout=15)
    assert stuck["status_source"] == "tmux"
    items = [i for i in (await api.get("/api/attention")).json() if i["session_id"] == session["id"]]
    assert items and "hasn't started" in items[0]["title"]
