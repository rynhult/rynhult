"""History search: each CLI's conversation file as messages, indexed and searched."""
from __future__ import annotations

import json
import os
import sqlite3
import threading

import pytest
from helpers import control, pause_supervisor, wait_for, write_db
from test_remote_spool import sup  # noqa: F401 (fixture)
from test_supervisor import launch, state_is

from acc.providers import transcripts
from acc.server.history import fts_query


@pytest.mark.parametrize("messages", [
    [{"role": "assistant", "text": "indexed answer\nGOAL: done"}], [], None,
])
async def test_index_waits_for_a_writer_without_blocking_the_event_loop(sup, messages):  # noqa: F811
    import asyncio

    from sqlalchemy import event, text

    from acc.server.db import WORKER_WRITE, WRITE_INTENT
    from acc.server.models import LaunchSpec, Project, Session

    with write_db(sup.db) as db:
        project = Project(slug="history", name="History", workspace_path="/w/history", account="agent")
        db.add(Session(id="history-race", project=project, provider="claude", name="History",
                       tmux_name="acc-history", run_as="agent", native_id="native",
                       launch_spec=LaunchSpec(provider="claude", inputs={}, files={}, digest="x")))
        db.execute(text("INSERT INTO transcript_fts (text, session_id, role, seq) "
                        "VALUES ('old answer', 'history-race', 'assistant', 0)"))
        db.commit()

    fetched = threading.Event()

    class Transcripts:
        async def call(self, method, **params):
            assert method == "transcript_messages"
            fetched.set()
            return {"found": True, "messages": messages, "stamp": "new"}

    sup.broker = Transcripts()
    engine = sup.db.kw["bind"]
    attempted = threading.Event()

    def beginning(connection, record, proxy):
        if fetched.is_set() and WORKER_WRITE.get():
            attempted.set()

    token = WRITE_INTENT.set(True)
    try:
        writer = engine.connect()
        writer.begin()
    finally:
        WRITE_INTENT.reset(token)
    try:
        writer.execute(text("UPDATE session SET automation=:value WHERE id='history-race'"),
                       {"value": json.dumps({"goal": {"text": "keep this edit"}})})
        event.listen(engine, "checkout", beginning)
        task = asyncio.create_task(sup.index_transcripts())
        try:
            assert await asyncio.wait_for(asyncio.to_thread(attempted.wait, 2), 3)
            writer.commit()  # must run on the event loop while indexing waits for this lock
            await asyncio.wait_for(task, 3)
        finally:
            writer.rollback()
            await asyncio.gather(task, return_exceptions=True)
            event.remove(engine, "checkout", beginning)
    finally:
        writer.close()

    with sup.db() as db:
        session = db.get(Session, "history-race")
        assert session.indexed_at is not None
        assert session.transcript_stamp == ("new" if messages is not None else None)
        assert session.automation["goal"] == {"text": "keep this edit"}
        stored = db.execute(text("SELECT text FROM transcript_fts WHERE session_id='history-race'")).scalars().all()
        assert stored == (["old answer"] if messages is None else [m["text"] for m in messages])
        if messages:
            assert session.automation["assistant_marker"] == {"seq": 0, "text": "GOAL: done"}


async def test_failed_index_replacement_rolls_back_messages_and_stamp(sup):  # noqa: F811
    from sqlalchemy import text
    from sqlalchemy.exc import StatementError

    from acc.server.models import LaunchSpec, Project, Session

    with write_db(sup.db) as db:
        db.add(Session(id="history-error", name="History", tmux_name="acc-history", provider="claude",
                       project=Project(slug="history", name="History", workspace_path="/w/history", account="agent"),
                       launch_spec=LaunchSpec(provider="claude", inputs={}, files={}, digest="x"),
                       native_id="native", run_as="agent", transcript_stamp="old"))
        db.execute(text("INSERT INTO transcript_fts (text, session_id, role, seq) "
                        "VALUES ('old answer', 'history-error', 'assistant', 0)"))
        db.commit()

    class Transcripts:
        async def call(self, method, **params):
            return {"found": True, "messages": [{"role": {}, "text": "invalid role"}], "stamp": "bad"}

    sup.broker = Transcripts()
    with pytest.raises(StatementError):
        await sup.index_transcripts()
    with sup.db() as db:
        session = db.get(Session, "history-error")
        assert session.transcript_stamp == "old" and session.indexed_at is None
        assert db.execute(text("SELECT text FROM transcript_fts WHERE session_id='history-error'")).scalar() == "old answer"


def jsonl(path, records):
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


def test_claude_keeps_words_not_tools(tmp_path):
    f = jsonl(tmp_path / "c.jsonl", [
        {"type": "user", "timestamp": "t1", "message": {"role": "user", "content": "Fix the login redirect"}},
        {"type": "assistant", "timestamp": "t2", "message": {"content": [{"type": "thinking", "thinking": "hmm"},
                                                                         {"type": "text", "text": "Fixed it."}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},  # tool output: skipped
        {"type": "user", "isMeta": True, "message": {"content": "<command-name>/clear</command-name>"}},
        {"type": "attachment"}, "not json",
    ])
    assert transcripts.claude(f) == [{"role": "user", "text": "Fix the login redirect", "ts": "t1"},
                                     {"role": "assistant", "text": "Fixed it.", "ts": "t2"}]


def test_codex_copilot_antigravity(tmp_path):
    codex = jsonl(tmp_path / "x.jsonl", [
        {"type": "response_item", "payload": {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "profile"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<environment_context>cwd</environment_context>"}]}},
        {"type": "response_item", "timestamp": "t", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "add caching"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Added."}]}},
    ])
    assert [(m["role"], m["text"]) for m in transcripts.codex(codex)] == [("user", "add caching"), ("assistant", "Added.")]
    copilot = jsonl(tmp_path / "events.jsonl", [
        {"type": "system.message", "data": {"content": "You are Copilot"}},
        {"type": "user.message", "data": {"content": "rename the module"}},
        {"type": "assistant.message", "data": {"content": ""}},  # tool requests only
        {"type": "assistant.message", "data": {"content": "Renamed."}},
    ])
    assert [m["text"] for m in transcripts.copilot(copilot)] == ["rename the module", "Renamed."]
    agy = jsonl(tmp_path / "transcript.jsonl", [
        {"type": "USER_INPUT", "created_at": "t", "content": "<USER_REQUEST>\nwrite docs\n</USER_REQUEST>\n<ADDITIONAL_METADATA>x</ADDITIONAL_METADATA>"},
        {"type": "PLANNER_RESPONSE", "content": "Wrote them."}, {"type": "GENERIC", "content": "command output"},
    ])
    assert [m["text"] for m in transcripts.antigravity(agy)] == ["write docs", "Wrote them."]


def test_cursor_store(tmp_path):
    db = sqlite3.connect(tmp_path / "store.db")
    db.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
    rows = [b'{"role":"system","content":"You are"}', b"\n\x08protobuf",
            json.dumps({"role": "user", "content": [{"type": "text", "text": "<timestamp>x</timestamp>\n<user_query>\nship it\n</user_query>"}]}).encode(),
            b'{"role":"assistant","content":"Shipped."}']
    db.executemany("INSERT INTO blobs VALUES (?, ?)", [(str(i), r) for i, r in enumerate(rows)])
    db.commit()
    assert [m["text"] for m in transcripts.cursor(tmp_path / "store.db")] == ["ship it", "Shipped."]


def test_query_is_safe():
    assert fts_query('deploy "the" NEAR(x') == '"deploy" "the" "NEAR" "x"'
    assert fts_query("login redir") == '"login" "redir"*'
    assert fts_query("  ;; ") is None


async def test_search_finds_a_conversation(api, project, env):
    session = await launch(api, project)
    await wait_for(state_is(api, session["id"], "idle"))
    control(env, session["id"], "say please migrate the billing webhooks to the new queue")

    async def found():
        results = (await api.get("/api/search", params={"q": "billing webhook"})).json()["results"]
        return results or None
    results = await wait_for(found)
    assert results[0]["session"]["id"] == session["id"] and results[0]["matches"] == 2
    assert "\x02billing\x03" in results[0]["hits"][0]["snippet"]
    assert (await api.get("/api/search", params={"q": "kubernetes"})).json()["results"] == []
    from acc.server import history
    with api.app.state.db() as s:  # someone who can't see that session finds nothing (the API passes ctx.scope)
        assert history.search(s, "billing", allowed={"someone-elses"}) == []
        assert history.search(s, "billing", allowed={session["id"]})[0]["matches"] == 2


async def test_search_scopes_before_limiting(api, project, env):
    """R4: other people's (or one long conversation's) matches can't crowd out the caller's results."""
    from sqlalchemy import text

    from acc.server import history
    # This test owns the index contents; live indexing must not replace the seeded messages.
    await pause_supervisor(api)
    mine = await launch(api, project)
    other = await launch(api, project)
    long_one = await launch(api, project)
    with write_db(api.app.state.db) as s:
        # 1,500 strong matches in a session the caller can't see, then one weak match of theirs
        history.replace(s, other["id"], [{"role": "user", "text": "billing billing billing"}] * 1500)
        history.replace(s, long_one["id"], [{"role": "user", "text": "billing billing"}] * 1200)
        history.replace(s, mine["id"], [{"role": "user", "text": "a long note that mentions billing once " + "x " * 50}])
        s.commit()
    with api.app.state.db() as s:
        assert s.execute(text("SELECT count(*) FROM transcript_fts")).scalar() == 2701
        only_mine = history.search(s, "billing", allowed={mine["id"]})
        assert [r["session"]["id"] for r in only_mine] == [mine["id"]] and only_mine[0]["matches"] == 1
        both = history.search(s, "billing", allowed={mine["id"], long_one["id"]})
        assert [r["session"]["id"] for r in both] == [long_one["id"], mine["id"]]  # the long one ranks first, but doesn't crowd
        assert both[0]["matches"] == 1200 and len(both[0]["hits"]) == 3
        assert len(history.search(s, "billing", allowed={mine["id"], long_one["id"], other["id"]}, limit=2)) == 2


async def test_search_filters_before_limiting_and_index_status_is_private(api, project):
    from datetime import datetime, timezone

    from test_users import browser

    from acc.server import auth, history
    from acc.server.models import LaunchSpec, Project, Session, User

    await pause_supervisor(api)
    with write_db(api.app.state.db) as s:
        second = Project(slug="search-other", name="Other", workspace_path="/w/search-other", account="agent")
        s.add(second)
        user = User(username="alex", role="member", account="agent", password_hash="unused", totp_secret="unused")
        s.add(user)
        s.flush()
        token, _ = auth.create_session(s, user, "test")
        fixtures = [
            ("filter-mine", project["id"], "alex", "claude", 20, "a note about billing"),
            ("filter-hidden", second.id, "riley", "claude", 23, "billing " * 500),
            ("filter-provider", project["id"], "alex", "codex", 22, "billing " * 100),
            ("filter-old", project["id"], "alex", "claude", 1, "billing " * 100),
            ("filter-project", second.id, "alex", "claude", 23, "billing " * 100),
            ("filter-empty", project["id"], "alex", "claude", 20, ""),
        ]
        for sid, pid, owner, provider, day, content in fixtures:
            s.add(Session(id=sid, project_id=pid, provider=provider, owner=owner, name=sid,
                          tmux_name=sid, run_as="agent", desired_state="stopped",
                          created_at=datetime(2026, 10, day, tzinfo=timezone.utc),
                          indexed_at=datetime(2026, 10, 24, tzinfo=timezone.utc),
                          launch_spec=LaunchSpec(provider=provider, inputs={}, files={}, digest=sid)))
            s.flush()
            history.replace(s, sid, [{"role": "user", "text": content}])
        s.commit()
    async with browser(api) as member:
        member.cookies.set("acc_session", token)
        filters = {"q": "billing", "project_id": project["id"], "provider": "claude", "since": "2026-10-15", "limit": 1}
        response = await member.get("/api/search", params=filters)
        assert response.status_code == 200, response.text
        data = response.json()
        assert [r["session"]["id"] for r in data["results"]] == ["filter-mine"]
        assert data["indexed_sessions"] == 1  # indexed_at alone is not an indexed conversation
        missing = (await member.get("/api/search", params={**filters, "q": "absentword"})).json()
        assert missing["results"] == [] and missing["indexed_sessions"] == 1
        assert (await member.get("/api/search", params={"q": "billing"})).json()["indexed_sessions"] == 4
        hidden = (await member.get("/api/search", params={"q": "billing", "project_id": 987654})).json()
        assert hidden["results"] == [] and hidden["indexed_sessions"] == 0
        invalid = await member.get("/api/search", params={**filters, "since": "not-a-date"})
        assert invalid.status_code == 422


# Input budgets (security review 2026-10-22, #4): inert fixtures, nothing is executed.

def test_oversized_line_is_skipped_and_reading_goes_on(tmp_path):
    huge = json.dumps({"type": "user", "message": {"content": "x" * transcripts.MAX_LINE}}) + "\n"
    f = tmp_path / "c.jsonl"
    f.write_text(json.dumps({"type": "user", "message": {"content": "before"}}) + "\n" + huge
                 + json.dumps({"type": "user", "message": {"content": "after"}}) + "\n")
    assert [m["text"] for m in transcripts.claude(f)] == ["before", "after"]


def test_reading_stops_at_the_byte_budget(tmp_path, monkeypatch):
    line = json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n"
    f = tmp_path / "c.jsonl"
    f.write_text(line * 100)
    monkeypatch.setattr(transcripts, "MAX_BYTES", len(line) * 10)
    assert len(transcripts.claude(f)) == 10


def test_reading_stops_at_max_messages(tmp_path, monkeypatch):
    monkeypatch.setattr(transcripts, "MAX_MESSAGES", 3)
    parsed = []
    real = transcripts.json.loads
    monkeypatch.setattr(transcripts.json, "loads", lambda raw: parsed.append(1) or real(raw))
    f = jsonl(tmp_path / "c.jsonl", [{"type": "user", "message": {"content": f"m{i}"}} for i in range(50)])
    assert [m["text"] for m in transcripts.claude(f)] == ["m0", "m1", "m2"]
    assert len(parsed) == 3  # the other 47 lines were never read


def cursor_store(path, blobs):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
    db.executemany("INSERT INTO blobs VALUES (?, ?)", [(str(i), b) for i, b in enumerate(blobs)])
    db.commit()
    db.close()
    return path


def test_cursor_too_many_rows_stops_at_max_messages(tmp_path, monkeypatch):
    monkeypatch.setattr(transcripts, "MAX_MESSAGES", 3)
    store = cursor_store(tmp_path / "store.db", [json.dumps({"role": "user", "content": f"m{i}"}).encode()
                                                 for i in range(200)])
    fetched = []
    real_connect = sqlite3.connect

    class Counting:  # counts the rows the parser actually pulls from SQLite
        def __init__(self, conn):
            self.conn = conn

        def execute(self, *args):
            for row in self.conn.execute(*args):
                fetched.append(1)
                yield row

        def close(self):
            self.conn.close()

    monkeypatch.setattr(transcripts.sqlite3, "connect", lambda *a, **k: Counting(real_connect(*a, **k)))
    assert [m["text"] for m in transcripts.cursor(store)] == ["m0", "m1", "m2"]
    assert len(fetched) == 3


def test_cursor_skips_oversized_blobs(tmp_path):
    big = json.dumps({"role": "user", "content": "x" * transcripts.MAX_LINE}).encode()
    store = cursor_store(tmp_path / "store.db", [b'{"role":"user","content":"small"}', big,
                                                 b'{"role":"assistant","content":"done"}'])
    assert [m["text"] for m in transcripts.cursor(store)] == ["small", "done"]


def test_fifo_and_symlinked_transcripts_are_not_read(tmp_path, monkeypatch):
    import os

    from acc.broker import ops
    fifo = tmp_path / "c.jsonl"
    os.mkfifo(fifo)  # a plain open with no writer would block forever
    for parse in (transcripts.claude, transcripts.codex, transcripts.copilot, transcripts.antigravity, transcripts.cursor):
        assert parse(fifo) == []
    real = jsonl(tmp_path / "real.jsonl", [{"type": "user", "message": {"content": "secret"}}])
    link = tmp_path / "link.jsonl"
    link.symlink_to(real)
    assert transcripts.claude(link) == []

    native = "11111111-2222-3333-4444-555555555555"
    chat = tmp_path / ".cursor" / "chats" / "h" / native
    chat.mkdir(parents=True)
    os.mkfifo(chat / "store.db")
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    assert ops.transcript_messages("cursor", native, None) == {"found": False}


def test_cursor_store_is_read_from_a_private_copy(tmp_path, monkeypatch):
    """SQLite opens by name: the reader copies store.db (and -wal) through checked descriptors and opens the copy,
    so a link or FIFO put where the store was is never opened by SQLite."""
    real = cursor_store(tmp_path / "real.db", [b'{"role":"user","content":"secret elsewhere"}'])
    (tmp_path / "chat").mkdir()
    (tmp_path / "chat" / "store.db").symlink_to(real)
    assert transcripts.cursor(tmp_path / "chat" / "store.db") == []
    os.mkfifo(tmp_path / "fifo.db")
    assert transcripts.cursor(tmp_path / "fifo.db") == []  # returns at once instead of blocking
    store = cursor_store(tmp_path / "store.db", [b'{"role":"user","content":"hi"}'])
    (tmp_path / "store.db-wal").symlink_to(real)  # a linked -wal is skipped, the store is still read
    opened = []
    real_connect = sqlite3.connect
    monkeypatch.setattr(transcripts.sqlite3, "connect", lambda p, *a, **k: opened.append(str(p)) or real_connect(p, *a, **k))
    assert [m["text"] for m in transcripts.cursor(store)] == ["hi"]
    assert opened and not opened[0].startswith(str(tmp_path))
    monkeypatch.setattr(transcripts, "MAX_STORE", 100)
    assert transcripts.cursor(store) == []  # too big to copy


async def test_sessions_backing_off_leave_the_history_pass_to_the_others(sup):  # noqa: F811 (fixture)
    """Q11: the 20 newest unreadable transcripts filled every slot of every pass, so an older readable one was never
    indexed."""
    from datetime import timedelta

    from acc.server import history
    from acc.server.models import LaunchSpec, Project, Session
    from acc.server.supervisor_common import now
    with sup.db() as db:
        project = Project(slug="p-local", name="local", host="local", workspace_path="/w/local", trusted=True, account="agent")
        db.add(project)
        for i in range(history.PER_PASS + 1):
            spec = LaunchSpec(provider="claude", inputs={}, files={}, digest="x")
            db.add(Session(id=f"hist{i:08d}", project=project, provider="claude", name=f"h{i}", tmux_name=f"acc-h{i}",
                           launch_spec=spec, run_as="agent", native_id=f"n{i}",
                           updated_at=now() - timedelta(minutes=i)))  # hist00000020 is the oldest
        db.commit()
    healthy = f"hist{history.PER_PASS:08d}"
    calls = []

    class Transcripts:
        async def call(self, method, **params):
            assert method == "transcript_messages"
            calls.append(params["native_id"])
            if params["native_id"] == f"n{history.PER_PASS}":
                return {"found": True, "messages": [{"role": "user", "text": "the older one"}], "stamp": "s"}
            return {"found": False}  # missing: backs off
    sup.broker = Transcripts()
    await sup.index_transcripts()
    assert len(calls) == history.PER_PASS and f"n{history.PER_PASS}" not in calls  # the newer ones came first
    calls.clear()
    await sup.index_transcripts()
    assert calls == [f"n{history.PER_PASS}"]  # the newer ones are backing off: the older one gets its turn
    with sup.db() as db:
        assert db.get(Session, healthy).indexed_at is not None
        assert all(db.get(Session, f"hist{i:08d}").indexed_at is None for i in range(history.PER_PASS))


@pytest.mark.parametrize("online_host", ["local", "connected-node"])
@pytest.mark.parametrize("team_override", [False, True])
async def test_disconnected_sessions_cannot_starve_history(sup, online_host, team_override):  # noqa: F811
    from datetime import timedelta
    from types import SimpleNamespace

    from acc.server import history
    from acc.server.models import LaunchSpec, Project, Session
    from acc.server.supervisor_common import now

    with sup.db() as db:
        away = Project(slug="away", name="Away", host="local" if team_override else "gone-node",
                       workspace_path="/w/away", account="agent")
        here = Project(slug="here", name="Here", host=online_host, workspace_path="/w/here", account="agent")
        for i in range(history.PER_PASS):
            db.add(Session(id=f"offline{i}", project=away, provider="claude", name="Offline", tmux_name=f"off{i}",
                           launch_spec=LaunchSpec(provider="claude", inputs={}, files={}, digest="x"),
                           run_as="agent", native_id=f"off{i}", team_host="gone-node" if team_override else None))
        db.add(Session(id="online", project=here, provider="claude", name="Online", tmux_name="online",
                       launch_spec=LaunchSpec(provider="claude", inputs={}, files={}, digest="x"),
                       run_as="agent", native_id="online", updated_at=now() - timedelta(days=1)))
        db.commit()

    class Transcripts:
        hub = SimpleNamespace(links={"connected-node": SimpleNamespace(closed=False)})

        async def call(self, method, **params):
            assert method == "transcript_messages"
            assert params["host"] == online_host and params["native_id"] == "online"
            return {"found": True, "messages": [{"role": "user", "text": "still searchable"}], "stamp": "s"}

    sup.broker = Transcripts()
    await sup.index_transcripts()
    await sup.index_transcripts()
    with sup.db() as db:
        assert db.get(Session, "online").indexed_at is not None
        assert history.search(db, "searchable", allowed={"online"})[0]["session"]["id"] == "online"
        assert all(db.get(Session, f"offline{i}").indexed_at is None for i in range(history.PER_PASS))


async def test_a_session_on_an_offline_machine_does_not_stop_the_pass(sup):  # noqa: F811 (fixture)
    """A transcript on a disconnected or removed machine backs off; it used to abort the whole supervisor pass, which
    skipped the stages after it and reported the broker as down."""
    from acc.server.broker_client import BrokerUnavailable
    from acc.server.models import LaunchSpec, Project, Session
    with sup.db() as db:
        away = Project(slug="p-away", name="away", host="gone-node", workspace_path="/w/away", trusted=True, account="agent")
        here = Project(slug="p-here", name="here", host="local", workspace_path="/w/here", trusted=True, account="agent")
        for i, project in enumerate((away, here)):
            db.add(Session(id=f"hostx{i:07d}", project=project, provider="claude", name=f"x{i}", tmux_name=f"acc-x{i}",
                           launch_spec=LaunchSpec(provider="claude", inputs={}, files={}, digest="x"), run_as="agent",
                           native_id=f"x{i}"))
        db.commit()
    local_down = False

    class Broker:
        async def call(self, method, **params):
            if params["host"] != "local" or local_down:
                raise BrokerUnavailable(f"host {params['host']} is not connected")
            return {"found": True, "messages": [{"role": "user", "text": "still indexed"}], "stamp": "s"}
    sup.broker = Broker()
    await sup.index_transcripts()
    with sup.db() as db:
        assert db.get(Session, "hostx0000000").indexed_at is None  # backing off, not marked as indexed
        assert db.get(Session, "hostx0000001").indexed_at is not None
    local_down = True  # this machine's own broker being down still stops the pass
    with sup.db() as db:
        db.get(Session, "hostx0000001").indexed_at = None
        db.commit()
    with pytest.raises(BrokerUnavailable):
        await sup.index_transcripts()


def history_session(supervisor, acc_id):
    from acc.server.models import LaunchSpec, Project, Session
    with write_db(supervisor.db) as db:
        db.add(Session(id=acc_id, name="History", tmux_name=f"acc-{acc_id}", provider="claude",
                       project=Project(slug=acc_id, name=acc_id, workspace_path=f"/w/{acc_id}", account="agent"),
                       launch_spec=LaunchSpec(provider="claude", inputs={}, files={}, digest="x"),
                       native_id="native", run_as="agent"))
        db.commit()


async def test_activity_during_a_read_leaves_the_session_due(sup):  # noqa: F811
    """A turn that ends while its transcript is being read isn't in what was read. Stamping indexed_at when the read
    is stored hid that activity (activity_at < indexed_at), so the newest messages stayed out of search until the
    session was active again. indexed_at is when the read began."""
    from acc.server.models import Session
    from acc.server.supervisor_common import now
    history_session(sup, "history-busy")

    class Transcripts:
        async def call(self, method, **params):
            with write_db(sup.db) as db:  # the session's activity lands mid-read
                db.get(Session, "history-busy").activity_at = now()
                db.commit()
            return {"found": True, "messages": [{"role": "user", "text": "first turn"}], "stamp": "s1"}
    sup.broker = Transcripts()
    await sup.index_transcripts()
    with sup.db() as db:
        session = db.get(Session, "history-busy")
        assert session.indexed_at is not None and session.activity_at > session.indexed_at  # due again


async def test_a_read_does_not_overwrite_an_index_state_changed_meanwhile(sup):  # noqa: F811
    """A pass whose read was in flight while indexed_at was changed (here reset to "never indexed") must not record
    its older read on top: the next pass reads again. This was the flake in test_unreadable_transcript_is_retried_not
    _marked_indexed, where the supervisor's own pass was mid-read when the test reset the session."""
    from datetime import timedelta

    from acc.server.models import Session
    from acc.server.supervisor_common import now
    history_session(sup, "history-reset")
    with write_db(sup.db) as db:
        db.get(Session, "history-reset").activity_at = now() - timedelta(minutes=1)
        db.commit()

    class Transcripts:
        async def call(self, method, **params):
            with write_db(sup.db) as db:
                db.get(Session, "history-reset").indexed_at = None if reads else now()  # someone else's change
                db.commit()
            reads.append(params["native_id"])
            return {"found": True, "messages": [{"role": "user", "text": "an answer"}], "stamp": "s1"}
    reads = []
    sup.broker = Transcripts()
    await sup.index_transcripts()  # first read: indexed_at changes under it (None -> a time)
    with sup.db() as db:
        changed = db.get(Session, "history-reset").indexed_at
    assert changed is not None
    with write_db(sup.db) as db:  # due again
        db.get(Session, "history-reset").activity_at = now()
        db.commit()
    await sup.index_transcripts()  # second read: reset to None under it
    with sup.db() as db:
        assert db.get(Session, "history-reset").indexed_at is None
