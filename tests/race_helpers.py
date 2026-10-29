"""Strict interleavings for database guard regressions, not scheduler-luck races."""
import asyncio
import threading
from contextlib import contextmanager

from sqlalchemy import create_engine, event

from acc.server.db import make_engine, make_sessionmaker, write_in_thread


async def during_broker_call(api, monkeypatch, method, sid, request, mutation):
    """Commit on a second connection after real broker work, before its reply is consumed."""
    original = api.app.state.supervisor.broker.call
    arrived, committed = asyncio.Event(), asyncio.Event()
    engine = make_engine(api.app.state.db.kw['bind'].url)
    db = make_sessionmaker(engine)

    async def held(called, **kwargs):
        result = await original(called, **kwargs)
        if called == method and kwargs.get('acc_id', kwargs.get('session_id')) == sid:
            arrived.set()
            await asyncio.wait_for(committed.wait(), 10)
        return result

    async def writer():
        await asyncio.wait_for(arrived.wait(), 10)
        try:
            await write_in_thread(mutation, db)
        finally:
            committed.set()

    try:
        with monkeypatch.context() as patch:
            patch.setattr(api.app.state.supervisor.broker, 'call', held)
            tasks = [asyncio.create_task(request), asyncio.create_task(writer())]
            try:
                results = await asyncio.gather(*tasks)
            finally:
                committed.set()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        return results[0]
    finally:
        engine.dispose()


@contextmanager
def guard_database(api, monkeypatch):
    """Isolate row guards from PostgreSQL's coarse API advisory writer lock.

    SQLite retains its production writer serialization. PostgreSQL uses independent
    READ COMMITTED transactions, so a missing CAS/fence cannot hide behind either
    the API advisory lock or the supervisor's REPEATABLE READ serialization error.
    Call only after pausing the supervisor and completing fixture setup.
    """
    engine = api.app.state.db.kw['bind']
    if engine.dialect.name == 'sqlite':
        yield api.app.state.db
        return
    concurrent = create_engine(engine.url, isolation_level='READ COMMITTED', hide_parameters=True)

    @event.listens_for(concurrent, 'begin')
    def deadline(connection):
        connection.exec_driver_sql("SET LOCAL lock_timeout = '10s'")
        connection.exec_driver_sql("SET LOCAL statement_timeout = '15s'")

    db = make_sessionmaker(concurrent)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(api.app.state.ctx, 'db', db)
            patch.setattr(api.app.state.supervisor, 'db', db)
            yield db
    finally:
        concurrent.dispose()


@contextmanager
def transaction_rendezvous(db, statement):
    """Meet before a guarded PostgreSQL write, or before SQLite writer admission.

    Only the first two arrivals participate. A missing participant is a test failure,
    never a tolerated timeout. Retaining the DBAPI objects proves separate connections.
    """
    engine = db.kw['bind']
    barrier = threading.Barrier(2, timeout=10)
    lock = threading.Lock()
    connections = []

    def meet(connection):
        with lock:
            if len(connections) == 2:
                return
            connections.append(connection)
        barrier.wait()

    def before_write(connection, _cursor, sql, _parameters, _context, _many):
        if sql.startswith(statement):
            meet(connection.connection.driver_connection)

    def checkout(connection, _record, _proxy):
        meet(connection)

    name, listener = ('checkout', checkout) if engine.dialect.name == 'sqlite' else (
        'before_cursor_execute', before_write)
    event.listen(engine, name, listener)
    try:
        yield
        assert len(connections) == 2, 'both competing transactions must reach the rendezvous'
        assert connections[0] is not connections[1], 'race requires independent database connections'
    finally:
        event.remove(engine, name, listener)
