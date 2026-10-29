"""The sessions list and the Inbox stay a fixed number of queries however many sessions exist (rc performance check:
2,000 sessions took ~6 s for the list, polled every 3 s, and the Inbox loaded every session to label 200 items)."""
import datetime as dt

import httpx
import perf_seed
import pytest
from fastapi.encoders import jsonable_encoder
from sqlalchemy import event, select, update

from acc.server.api.schemas import session_out
from acc.server.app import create_app
from acc.server.models import AttentionItem, Node, Session

SCALE = dict(people=3, projects=12, sessions=150, events=600, long_sessions=2, long_events=50, audit=50,
             inbox=40, machines=6, running=0)


@pytest.fixture
async def seeded(env):
    settings = env["settings"]
    perf_seed.seed(settings.db_url, settings.data_dir, SCALE)
    app = create_app(settings)  # the supervisor isn't started: nothing reconciles the synthetic sessions
    engine = app.state.db.kw["bind"]
    with app.state.db() as s:
        # machine-02 is offline; the owner (the API token) sees Inbox items that belong to nobody
        s.execute(update(Node).where(Node.name == "machine-02").values(offline_since=dt.datetime.now(dt.timezone.utc)))
        s.execute(update(AttentionItem).values(owner=None))
        s.commit()
    statements = []
    event.listen(engine, "before_cursor_execute", lambda conn, cursor, sql, *a: statements.append(sql))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://acc",
                                 headers={"Authorization": f"Bearer {settings.api_token()}"}) as client:
        client.app, client.statements = app, statements
        yield client
    await app.state.supervisor.broker.close()
    engine.dispose()


async def test_session_list_is_a_fixed_number_of_queries_and_the_same_bytes(seeded):
    seeded.statements.clear()
    response = await seeded.get("/api/sessions")
    assert response.status_code == 200 and response.headers["content-type"] == "application/json"
    listed = response.json()
    assert len(listed) == SCALE["sessions"]
    selects = [sql for sql in seeded.statements if sql.lstrip().upper().startswith("SELECT")]
    assert len(selects) <= 8, selects  # was one per session on another machine, plus lazy loads
    # The same JSON as the per-row encoding it replaces.
    with seeded.app.state.db() as s:
        expected = jsonable_encoder([session_out(x) for x in s.scalars(select(Session).order_by(Session.created_at.desc()))])
    assert listed == expected
    offline = {x["id"] for x in listed if x["machine_offline"]}
    assert offline and all(x["host"] == "machine-02" for x in listed if x["id"] in offline)
    assert all(x["machine_offline"] for x in listed if x["host"] == "machine-02")


async def test_inbox_reads_only_the_listed_items_sessions(seeded):
    seeded.statements.clear()
    response = await seeded.get("/api/attention?scope=pending")
    assert response.status_code == 200
    items = response.json()
    assert items and all(item["session_name"] for item in items if item.get("session_id"))
    session_reads = [sql for sql in seeded.statements if "FROM session" in sql and "attention_item" not in sql]
    assert len(session_reads) == 1 and " IN " in session_reads[0].upper()  # not every session


async def test_an_unchanged_session_list_is_an_empty_304(seeded):
    first = await seeded.get("/api/sessions")
    etag = first.headers["etag"]
    assert first.headers["cache-control"] == "private, no-cache"
    again = await seeded.get("/api/sessions", headers={"If-None-Match": etag})
    assert again.status_code == 304 and again.content == b"" and again.headers["etag"] == etag
    with seeded.app.state.db() as s:
        s.execute(update(Session).where(Session.name == "task-0").values(name="renamed"))
        s.commit()
    changed = await seeded.get("/api/sessions", headers={"If-None-Match": etag})
    assert changed.status_code == 200 and changed.headers["etag"] != etag
    assert any(x["name"] == "renamed" for x in changed.json())
