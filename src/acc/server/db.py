from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from contextlib import closing
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from weakref import WeakValueDictionary

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from ..maintenance import lease

MIGRATIONS = Path(__file__).parent / "migrations"
log = logging.getLogger(__name__)

# Set for mutating API requests (app.write_intent middleware). Their transactions begin with BEGIN IMMEDIATE,
# taking the write lock up front so they wait (busy_timeout) instead of failing with "database is locked":
# in WAL mode a read transaction that later writes cannot be upgraded once another writer has committed.
# Reads keep a plain BEGIN. Supervisor code that awaits a broker stays deferred: waiting for a write lock on the
# event loop could freeze the API request holding it. Short, database-only mutations use write_in_thread instead.
WRITE_INTENT: ContextVar[bool] = ContextVar("acc_db_write_intent", default=False)
# Only short synchronous worker bodies use this: never hold it across an await.
WORKER_WRITE: ContextVar[bool] = ContextVar("acc_db_worker_write", default=False)


class _TransactionGate:
    """Keep short worker commits from invalidating existing deferred snapshots.

    Ordinary transactions may overlap as before. A worker enters only when all
    have finished; while it runs, new transactions wait for its database-only body.
    """
    def __init__(self):
        self.condition = threading.Condition()
        self.readers = 0
        self.worker = False

    def enter(self, worker: bool, timeout: float):
        with self.condition:
            if not self.condition.wait_for(
                lambda: not self.worker and (not worker or not self.readers), timeout
            ):
                error = sqlite3.OperationalError("database is locked")
                error.sqlite_errorcode = sqlite3.SQLITE_BUSY
                error.sqlite_errorname = "SQLITE_BUSY"
                raise OperationalError("BEGIN", (), error)
            if worker:
                self.worker = True
            else:
                self.readers += 1

        def release():
            with self.condition:
                if worker:
                    self.worker = False
                else:
                    self.readers -= 1
                self.condition.notify_all()
        return release


_gates: WeakValueDictionary[str, _TransactionGate] = WeakValueDictionary()
_gates_lock = threading.Lock()


def _transaction_gate(engine: Engine) -> _TransactionGate:
    # Different engines pointing at the same file must participate in the same gate.
    key = str(Path(engine.url.database).resolve())
    with _gates_lock:
        gate = _gates.get(key)
        if gate is None:
            gate = _gates[key] = _TransactionGate()
        return gate


class _SQLiteConnection(sqlite3.Connection):
    release_transaction = None

    def _release(self):
        release, self.release_transaction = self.release_transaction, None
        if release is not None:
            release()

    def commit(self):
        super().commit()
        self._release()

    def rollback(self):
        super().rollback()
        self._release()

    def close(self):
        try:
            super().close()
        finally:
            self._release()


def write_intent(method):
    """Worker/API thread mutations reserve the writer before their first read."""
    @wraps(method)
    def call(*args, **kwargs):
        token = WRITE_INTENT.set(True)
        worker_token = WORKER_WRITE.set(True)
        try:
            return method(*args, **kwargs)
        finally:
            WORKER_WRITE.reset(worker_token)
            WRITE_INTENT.reset(token)
    return call


async def write_in_thread(work, *args):
    """Run a short database-only mutation off-loop; cancellation joins its transaction."""
    worker = asyncio.create_task(asyncio.to_thread(write_intent(work), *args))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break  # inspect and report the completed worker's failure below
        try:
            worker.result()
        except Exception:
            log.exception("database worker failed during cancellation")
        raise


class MaintenanceBusy(TimeoutError):
    """Retry after capture releases its short application mutation barrier."""


# Manual HA: the session lock only one running controller holds (ha.py), also taken by
# a startup schema change so nothing is promoted while the schema moves.
LEADER_LOCK = 1094927183
# Manual HA: notice a partitioned peer in about half a minute on both ends, so a dead
# main's session locks don't block fenced recovery for hours (default server keepalive
# is two hours), and a check on a dead socket can't hang for the kernel's ~15 minutes.
KEEPALIVE = {"keepalives": 1, "keepalives_idle": 10, "keepalives_interval": 5, "keepalives_count": 3,
             "tcp_user_timeout": 30000}
SERVER_KEEPALIVE = ("-c tcp_keepalives_idle=10 -c tcp_keepalives_interval=5 -c tcp_keepalives_count=3 "
                    "-c tcp_user_timeout=30000")


def _postgres_engine(url: str, keepalive: bool = False, **options) -> Engine:
    parsed = make_url(url)
    if "sslmode" not in parsed.query:
        parsed = parsed.update_query_dict({"sslmode": "verify-full"})
    if keepalive:
        connect_args = dict(options.pop("connect_args", {}))
        given = parsed.query.get("options", "")
        given = " ".join(given) if isinstance(given, tuple) else given
        # connect_args replace the URL's own libpq options: keep the owner's.
        merged = " ".join(part for part in (given, connect_args.get("options", ""), SERVER_KEEPALIVE) if part)
        options["connect_args"] = {**connect_args, **KEEPALIVE, "options": merged}
    return create_engine(parsed, pool_pre_ping=True, hide_parameters=True, **options)


def _sqlite_engine(url: str, *, foreign_keys: bool = True, **options) -> Engine:
    connect_args = {"check_same_thread": False, "timeout": 15, "factory": _SQLiteConnection,
                    **options.pop("connect_args", {})}
    engine = create_engine(url, connect_args=connect_args, **options)

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_connection, _record):
        with closing(dbapi_connection.cursor()) as cursor:
            cursor.execute("PRAGMA busy_timeout=15000")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute(f"PRAGMA foreign_keys={'ON' if foreign_keys else 'OFF'}")

    return engine


def make_engine(url: str, keepalive: bool = False) -> Engine:
    # isolation_level=None: the driver starts no transaction on its own; the "begin" hook below does.
    if make_url(url).get_backend_name() == "postgresql":
        engine = _postgres_engine(url, keepalive=keepalive)

        @event.listens_for(engine, "begin")
        def _postgres_begin(connection):
            # Preserve serialized API read/modify/write operations. Background stages must
            # fail quickly on row contention: a synchronous wait would block the event loop
            # while the API holding the row awaits a broker. Its next pass retries.
            connection.exec_driver_sql("SET LOCAL lock_timeout = '15s'" if WRITE_INTENT.get()
                                       else "SET LOCAL lock_timeout = '1ms'")
            if WRITE_INTENT.get():
                connection.exec_driver_sql("SELECT pg_advisory_xact_lock(1094927182)")
            else:
                # Reject stale writes to rows changed by a concurrent transaction.
                # Unlike SQLite, disjoint-row writes do not invalidate this snapshot:
                # authorization must be refreshed after broker waits before launch.
                # API transactions retain READ COMMITTED: their advisory-lock SELECT
                # must not pin a snapshot from before the preceding writer commits.
                connection.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")

        return engine
    if make_url(url).get_backend_name() != "sqlite":
        raise ValueError("Rynhult supports only SQLite and PostgreSQL")
    engine = _sqlite_engine(url, connect_args={"isolation_level": None})
    gate = _transaction_gate(engine)

    @event.listens_for(engine, "begin")
    def _begin(connection):
        raw = connection.connection.driver_connection
        timeout_ms = raw.execute("PRAGMA busy_timeout").fetchone()[0]
        path = Path(engine.url.database).parent / '.maintenance.lock'
        # Synchronous supervisor DB work must never sleep on the event loop.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            timeout = 30
            off_loop_writer = WRITE_INTENT.get()
        else:
            timeout = 0
            off_loop_writer = False
        barrier = lease(path, exclusive=False, timeout=timeout)
        try:
            barrier.__enter__()
        except TimeoutError:
            raise MaintenanceBusy('Backup capture or maintenance is busy; retry shortly') from None
        # Maintenance comes first: a worker queued behind capture must not own
        # admission and make a new event-loop transaction wait for that capture.
        try:
            release_gate = gate.enter(WORKER_WRITE.get() or off_loop_writer, timeout_ms / 1000)
        except BaseException:
            barrier.__exit__(None, None, None)
            raise

        def release():
            try:
                barrier.__exit__(None, None, None)
            finally:
                release_gate()

        raw.release_transaction = release
        try:
            connection.exec_driver_sql("BEGIN IMMEDIATE" if WRITE_INTENT.get() else "BEGIN")
        except BaseException:
            raw._release()
            raise

    return engine


def alembic_config(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


def migrate(url: str) -> None:
    config = alembic_config(url)
    if make_url(url).get_backend_name() == "postgresql":
        # DDL waits independently of the application's background lock timeout.
        engine = _postgres_engine(url)
        try:
            with engine.connect() as connection:
                # Session lock survives migration commits and serializes startup DDL.
                connection.exec_driver_sql("SELECT pg_advisory_lock(1094927181)")
                connection.commit()
                try:
                    config.attributes["connection"] = connection
                    command.upgrade(config, "head")
                finally:
                    connection.rollback()
                    connection.exec_driver_sql("SELECT pg_advisory_unlock(1094927181)")
                    connection.commit()
        finally:
            engine.dispose()
        return
    database = Path(make_url(url).database)
    before = None
    if database.is_file():
        # Restarting an already-current app is read-only. Taking an exclusive lease here
        # can deadlock a live transaction whose coroutine needs this event loop to finish.
        try:
            with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as connection:
                rows = connection.execute("select version_num from alembic_version").fetchall()
            if rows == [(ScriptDirectory.from_config(config).get_current_head(),)]:
                return
            before = rows[0][0] if len(rows) == 1 and isinstance(rows[0][0], str) else None
        except sqlite3.Error:
            pass  # new/partial databases still go through the serialized migration path
    with lease(database.parent / '.maintenance.lock'):
        if before is not None:
            pre_migration_copy(database, before)
        # Alembic batch rebuilds need foreign keys disabled while replacing referenced tables.
        engine = _sqlite_engine(url, foreign_keys=False)
        try:
            with engine.connect() as connection:
                config.attributes["connection"] = connection
                command.upgrade(config, "head")
        finally:
            engine.dispose()


def pre_migration_copy(database: Path, revision: str) -> Path | None:
    """A consistent copy of the database before its schema is upgraded: acc.db.pre-migration-<revision>, private to
    accd's account. The updater takes a full backup first, but an upgrade by hand (a release candidate installed over
    the last one) only had the daily backup (install and update review, IU-1). The first copy from a revision is kept;
    a failed migration retried later never overwrites it with a half-migrated database."""
    import os
    import re
    import stat
    if not re.fullmatch(r"[0-9A-Za-z_]{1,32}", revision):
        return None
    target = database.with_name(f"{database.name}.pre-migration-{revision}")
    if target.exists() or target.is_symlink():
        try:
            info = target.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ValueError("unsafe retained copy")
            # A completed SQLite backup is self-contained; don't replay a neighbouring WAL or create sidecars.
            with closing(sqlite3.connect(target.absolute().as_uri() + "?mode=ro&immutable=1", uri=True)) as copy:
                if copy.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise ValueError("invalid retained copy")
                if copy.execute("SELECT version_num FROM alembic_version").fetchall() != [(revision,)]:
                    raise ValueError("wrong retained revision")
        except (OSError, ValueError, sqlite3.Error):
            raise RuntimeError("existing pre-migration copy is unsafe or invalid; leave it in place and review it "
                               "before retrying the upgrade") from None
        return target
    staging = database.with_name(f".{database.name}.pre-migration-{revision}.tmp")
    try:
        staging.unlink()
    except FileNotFoundError:
        pass
    os.close(os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600))
    try:
        with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as source, \
                closing(sqlite3.connect(staging)) as copy:
            source.backup(copy)
        fd = os.open(staging, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(staging, target)
    except BaseException:
        staging.unlink(missing_ok=True)
        raise
    log.info("database copied before migrating from revision %s: %s", revision, target.name)
    return target


def make_sessionmaker(engine: Engine) -> sessionmaker:
    return sessionmaker(engine, expire_on_commit=False)
