"""SQLite write intent: mutating requests take the write lock up front instead of failing on upgrade."""
from __future__ import annotations

import threading
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from acc.server.db import WRITE_INTENT, make_engine, migrate, write_in_thread


@pytest.fixture
def engine(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'race.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE t (v INTEGER)"))
    yield engine
    engine.dispose()


def test_a_deferred_read_cannot_upgrade_after_another_commit(engine):
    """The original 'database is locked': read first, someone else writes, then try to write."""
    with engine.connect() as reader:
        tx = reader.begin()
        reader.execute(text("SELECT count(*) FROM t")).scalar()  # read snapshot taken
        with engine.begin() as other:
            other.execute(text("INSERT INTO t VALUES (1)"))
        with pytest.raises(OperationalError, match="locked") as error:
            reader.execute(text("INSERT INTO t VALUES (2)"))
        assert error.value.orig.sqlite_errorcode == 517  # SQLITE_BUSY_SNAPSHOT, not a timeout
        tx.rollback()


def test_write_intent_waits_its_turn_instead(engine):
    token = WRITE_INTENT.set(True)
    try:
        with engine.connect() as first:
            tx = first.begin()  # BEGIN IMMEDIATE: holds the write lock now
            first.execute(text("SELECT count(*) FROM t")).scalar()
            result = {}

            def second_writer():
                WRITE_INTENT.set(True)
                with engine.begin() as second:  # waits (busy_timeout) for the first to commit
                    second.execute(text("INSERT INTO t VALUES (2)"))
                result["ok"] = True

            thread = threading.Thread(target=second_writer)
            thread.start()
            first.execute(text("INSERT INTO t VALUES (1)"))
            tx.commit()
            thread.join(timeout=10)
        assert result == {"ok": True}
        with engine.connect() as check:
            assert check.execute(text("SELECT count(*) FROM t")).scalar() == 2
    finally:
        WRITE_INTENT.reset(token)


def test_authority_pool_uses_the_same_sqlite_connection_settings(tmp_path):
    from acc.server.ha import Authority

    url = f"sqlite:///{tmp_path / 'authority.db'}"
    migrate(url)
    engine = make_engine(url)
    authority = Authority(engine, "main-test")
    try:
        # Keep both checked out: the second must be a newly created DBAPI connection.
        with engine.connect() as app, authority.control.connect() as first, authority.control.connect() as second:
            for pragma in ("busy_timeout", "foreign_keys", "journal_mode"):
                expected = app.exec_driver_sql(f"PRAGMA {pragma}").scalar()
                assert first.exec_driver_sql(f"PRAGMA {pragma}").scalar() == expected
                assert second.exec_driver_sql(f"PRAGMA {pragma}").scalar() == expected
    finally:
        authority.control.dispose()
        engine.dispose()


def test_migrations_configure_sqlite_before_acquiring_the_writer(tmp_path, engine):
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    pragmas = ("busy_timeout", "journal_mode")
    with engine.connect() as app:
        expected = [app.exec_driver_sql(f"PRAGMA {p}").scalar() for p in pragmas]
    settings = []

    def beginning(connection, cursor, statement, parameters, context, many):
        if statement == "BEGIN IMMEDIATE":
            settings.append([connection.exec_driver_sql(f"PRAGMA {p}").scalar() for p in pragmas])

    event.listen(Engine, "before_cursor_execute", beginning)
    try:
        migrate(f"sqlite:///{tmp_path / 'migration.db'}")
    finally:
        event.remove(Engine, "before_cursor_execute", beginning)
    assert settings == [expected]


async def test_cancelled_database_worker_is_joined_before_returning(engine):
    import asyncio

    started, release = threading.Event(), threading.Event()

    def work():
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO t VALUES (1)"))
            started.set()
            assert release.wait(3)

    task = asyncio.create_task(write_in_thread(work))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(started.wait, 2), 3)
        task.cancel()
        await asyncio.sleep(0)  # deliver cancellation while the worker owns its transaction
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    with engine.connect() as connection:
        assert connection.execute(text("SELECT v FROM t")).scalars().all() == [1]


async def test_worker_failure_during_cancellation_preserves_shutdown(engine, caplog):
    import asyncio

    started, release = threading.Event(), threading.Event()

    def work():
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO t VALUES (1)"))
            started.set()
            assert release.wait(3)
            connection.execute(text("INSERT INTO missing_table VALUES (2)"))

    task = asyncio.create_task(write_in_thread(work))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(started.wait, 2), 3)
        task.cancel()
        await asyncio.sleep(0)
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "no such table: missing_table" in caplog.text
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM t")).scalar() == 0


async def test_repeated_cancellation_still_joins_the_database_worker(engine):
    import asyncio

    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def work():
        try:
            with engine.begin() as connection:
                connection.execute(text("INSERT INTO t VALUES (1)"))
                started.set()
                assert release.wait(3)
        finally:
            finished.set()

    task = asyncio.create_task(write_in_thread(work))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(started.wait, 2), 3)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)  # deliver the worker task's cancellation back to its caller
        assert not task.done()
    finally:
        release.set()
        assert await asyncio.wait_for(asyncio.to_thread(finished.wait, 2), 3)
        await asyncio.gather(task, return_exceptions=True)
    with pytest.raises(asyncio.CancelledError):
        await task
    with engine.connect() as connection:
        assert connection.execute(text("SELECT v FROM t")).scalars().all() == [1]


async def test_database_worker_surfaces_lock_timeout_without_replaying(engine):
    from sqlalchemy import event

    def short_timeout(dbapi_connection, record, proxy):
        dbapi_connection.execute("PRAGMA busy_timeout=50")

    attempts = []

    def work():
        attempts.append("write")
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO t VALUES (2)"))

    with engine.begin() as writer:
        writer.execute(text("INSERT INTO t VALUES (1)"))
        event.listen(engine, "checkout", short_timeout)
        try:
            with pytest.raises(OperationalError, match="locked"):
                await write_in_thread(work)
        finally:
            event.remove(engine, "checkout", short_timeout)
    assert attempts == ["write"]
    with engine.connect() as connection:
        assert connection.execute(text("SELECT v FROM t")).scalars().all() == [1]


@pytest.mark.parametrize("finish", ["commit", "rollback", "close"])
@pytest.mark.parametrize("separate_engine", [False, True])
async def test_background_writer_cannot_invalidate_an_unmarked_read_transaction(engine, finish, separate_engine):
    """A direct session/GET/supervisor read must still be able to write after a worker starts."""
    import asyncio

    from acc.server.db import make_sessionmaker

    worker_engine = make_engine(str(engine.url)) if separate_engine else engine
    started = threading.Event()
    done = threading.Event()

    def work():
        started.set()
        try:
            with worker_engine.begin() as connection:
                connection.execute(text("INSERT INTO t VALUES (2)"))
        finally:
            done.set()

    db = make_sessionmaker(engine)()
    db.execute(text("SELECT count(*) FROM t")).scalar()
    task = asyncio.create_task(write_in_thread(work))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        # The worker may attempt BEGIN, but cannot commit across this read snapshot.
        assert not await asyncio.to_thread(done.wait, 0.1)
        db.execute(text("INSERT INTO t VALUES (1)"))
        getattr(db, finish)()
    finally:
        db.close()
        try:
            await asyncio.wait_for(task, 3)
        finally:
            if separate_engine:
                worker_engine.dispose()
    with engine.connect() as check:
        assert check.execute(text("SELECT v FROM t ORDER BY v")).scalars().all() == (
            [1, 2] if finish == "commit" else [2]
        )


async def test_failed_maintenance_entry_does_not_leave_a_worker_reservation(engine, monkeypatch):
    from contextlib import contextmanager

    from acc.server import db as database

    @contextmanager
    def refused(*args, **kwargs):
        raise OSError("cannot open maintenance lock")
        yield

    def work():
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO t VALUES (1)"))

    with monkeypatch.context() as patch:
        patch.setattr(database, "lease", refused)
        with pytest.raises(OSError, match="cannot open maintenance lock"):
            await write_in_thread(work)
    # A leaked admission must fail quickly rather than making this test wait 15s.
    from sqlalchemy import event

    def timeout(connection, record, proxy):
        connection.execute("PRAGMA busy_timeout=50")

    event.listen(engine, "checkout", timeout)
    try:
        await write_in_thread(work)
    finally:
        event.remove(engine, "checkout", timeout)
    with engine.connect() as check:
        assert check.execute(text("SELECT v FROM t")).scalars().all() == [1]


async def test_worker_waiting_for_capture_does_not_hold_up_event_loop_admission(engine, monkeypatch):
    import asyncio
    from contextlib import contextmanager

    from sqlalchemy import event

    from acc.maintenance import lease
    from acc.server import db as database

    attempted = threading.Event()

    @contextmanager
    def observed_lease(*args, **kwargs):
        if database.WORKER_WRITE.get():
            attempted.set()
        with lease(*args, **kwargs):
            yield

    def short_timeout(connection, record, proxy):
        connection.execute("PRAGMA busy_timeout=50")

    def work():
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO t VALUES (1)"))

    monkeypatch.setattr(database, "lease", observed_lease)
    event.listen(engine, "checkout", short_timeout)
    task = None
    try:
        with lease(Path(engine.url.database).parent / '.maintenance.lock'):
            task = asyncio.create_task(write_in_thread(work))
            assert await asyncio.to_thread(attempted.wait, 2)
            # Capture must stay retryable immediately, even with a queued worker.
            with pytest.raises(database.MaintenanceBusy):
                with engine.begin():
                    pass
    finally:
        if task is not None:
            await asyncio.wait_for(task, 3)
        event.remove(engine, "checkout", short_timeout)
    with engine.connect() as check:
        assert check.execute(text("SELECT v FROM t")).scalars().all() == [1]


async def test_plain_write_intent_thread_cannot_invalidate_loop_snapshot(engine):
    import asyncio

    attempted, done = threading.Event(), threading.Event()

    def api_thread():
        token = WRITE_INTENT.set(True)  # middleware context, without the worker decorator
        attempted.set()
        try:
            with engine.begin() as connection:
                connection.execute(text("INSERT INTO t VALUES (2)"))
        finally:
            WRITE_INTENT.reset(token)
            done.set()

    with engine.connect() as reader:
        reader.execute(text("SELECT count(*) FROM t"))
        task = asyncio.create_task(asyncio.to_thread(api_thread))
        try:
            assert await asyncio.to_thread(attempted.wait, 2)
            assert not await asyncio.to_thread(done.wait, .1)
            reader.execute(text("INSERT INTO t VALUES (1)"))
            reader.commit()
        finally:
            reader.rollback()
            await asyncio.wait_for(task, 3)
    with engine.connect() as check:
        assert check.execute(text("SELECT v FROM t ORDER BY v")).scalars().all() == [1, 2]
