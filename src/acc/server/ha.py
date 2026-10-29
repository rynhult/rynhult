"""Explicit authority only. Database loss never elects a replacement.

The leadership connection excludes other running controllers. Activity locks drain
transactions and external operations before an epoch can change. Epoch checks use a
fresh connection: a repeatable-read application's old snapshot is not authority.
"""
from __future__ import annotations

import re
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

from alembic.script import ScriptDirectory
from sqlalchemy import event, inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from .db import LEADER_LOCK, MIGRATIONS, _postgres_engine, _sqlite_engine
from .models import AuditLog

ACTIVITY_LOCK = 1094927184
# The epoch a request, socket or worker was admitted under. Tasks and to_thread work
# inherit it, so work that outlives a demotion stays fenced after a later re-promotion.
ADMITTED: ContextVar[int | None] = ContextVar('acc_ha_admitted_epoch', default=None)


class Fenced(RuntimeError):
    """This main cannot perform an action; inspect HA status and retry on the active main."""


class Authority:
    def __init__(self, engine, node_id: str):
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,39}', node_id):
            raise ValueError('ACC_HA_NODE_ID must be a stable lowercase name (up to 40 characters)')
        self.engine = engine
        # Authority reads must not inherit the caller's API transaction lock or
        # repeatable-read snapshot: that would deadlock against its own write.
        self.control = (_postgres_engine(engine.url, keepalive=True, poolclass=NullPool,
                                         connect_args={'connect_timeout': 5,
                                                       'options': '-c statement_timeout=3000 -c lock_timeout=1000'})
                        if engine.dialect.name == 'postgresql'
                        else _sqlite_engine(engine.url))
        self.node_id = node_id
        self.incarnation = uuid.uuid4().hex
        self.epoch = None
        self.leader = None
        self.accepting = False
        self.uncertain = False
        self.lock = threading.RLock()
        self.activities = 0
        self.activity_connection = None
        self.postgres = engine.dialect.name == 'postgresql'
        self.auto_evaluate = False
        self._public = None
        with self.control.begin() as connection:
            connection.execute(text('INSERT INTO ha_authority (id, epoch) VALUES (1, 0) ON CONFLICT (id) DO NOTHING'))

    def _row(self, connection):
        return connection.execute(text('SELECT epoch, holder, incarnation, uncertain FROM ha_authority WHERE id=1')).mappings().one()

    def status(self):
        if self.accepting:
            try:
                self.check()
            except Fenced:
                pass
        with self.control.connect() as connection:
            row = self._row(connection)
        active = (self.accepting and self.epoch == row['epoch'] and row['incarnation'] == self.incarnation)
        return {'enabled': True, 'node': self.node_id, 'role': 'active' if active else 'standby',
                'epoch': row['epoch'], 'active_node': row['holder'], 'database': 'ok',
                # A failed persistence attempt must not hide the current holder's
                # fail-closed local latch from either owner or public status.
                'automatic_failover': False, 'uncertain': bool(row['uncertain'] or (self.uncertain
                    and self.epoch == row['epoch'] and row['incarnation'] == self.incarnation)),
                'automatic_evaluation': {'evaluation_enabled': self.auto_evaluate,
                    'would_promote': False, 'promotion_wired': False,
                    'reason': 'telemetry_unavailable' if self.auto_evaluate else 'disabled'}}

    def public_status(self):
        """The unauthenticated health projection, at most one database read per second."""
        now = time.monotonic()
        if self._public is None or now - self._public[0] >= 1:
            try:
                status = self.status()
                ha = {key: status[key] for key in ('enabled', 'role', 'epoch', 'database', 'automatic_failover', 'uncertain', 'automatic_evaluation')}
            except Exception:
                ha = {'enabled': True, 'role': 'standby', 'database': 'unavailable', 'automatic_failover': False}
            self._public = now, ha
        return self._public[1]

    def check(self):
        admitted = ADMITTED.get()
        if admitted is not None and admitted != self.epoch:
            # Only this work is stale; the current epoch keeps serving.
            raise Fenced('Work admitted under an earlier epoch; use the active main')
        with self.lock:
            self._check()

    def _check(self):
        if not self.accepting or self.epoch is None:
            raise Fenced('Standby is read-only; use the active main')
        try:
            if self.leader is None or self.leader.closed or self.leader.invalidated:
                raise Fenced('Leadership connection lost')
            # This retained connection is independent of application snapshots;
            # committing each check keeps the next epoch read fresh.
            row = self._row(self.leader)
            self.leader.commit()
            if self.activity_connection is not None:
                if self.activity_connection.closed or self.activity_connection.invalidated:
                    raise Fenced('Activity connection lost')
                self.activity_connection.execute(text('SELECT 1'))
                self.activity_connection.commit()
            # An uncertain effect blocks clean handover (demote), not the current holder:
            # a lost node reply must not stop every other machine and person.
            if row['epoch'] != self.epoch or row['incarnation'] != self.incarnation:
                raise Fenced('Stale authority epoch; use the active main')
        except Exception as error:
            self.accepting = False
            raise Fenced('Authority unavailable; control actions are stopped') from error

    @contextmanager
    def activity(self):
        # One shared session lock per main, even with many node sockets/requests.
        # Reference counting keeps it held until the final admitted action drains.
        # A standby (or a draining main) refuses locally: unauthenticated traffic must
        # never hold the cluster-wide barrier that promotion and demotion need.
        if not self.accepting or self.epoch is None:
            raise Fenced('Standby is read-only; use the active main')
        with self.lock:
            if self.activities == 0 and self.postgres:
                connection = self.control.connect()
                try:
                    if not connection.execute(text('SELECT pg_try_advisory_lock_shared(:key)'),
                                              {'key': ACTIVITY_LOCK}).scalar():
                        raise Fenced('Authority handover in progress; retry')
                    connection.commit()
                    self.activity_connection = connection
                except BaseException:
                    connection.close()
                    raise
            self.activities += 1
        token = ADMITTED.set(self.epoch) if ADMITTED.get() is None else None
        try:
            self.check()
            yield
        finally:
            if token is not None:
                ADMITTED.reset(token)
            with self.lock:
                self.activities -= 1
                if self.activities == 0 and self.activity_connection is not None:
                    connection, self.activity_connection = self.activity_connection, None
                    try:
                        connection.rollback()
                        connection.execute(text('SELECT pg_advisory_unlock_shared(:key)'), {'key': ACTIVITY_LOCK})
                        connection.commit()
                    except SQLAlchemyError:
                        self.accepting = False
                        connection.invalidate()
                    finally:
                        connection.close()

    def promote(self, *, expected_epoch: int, old_main_fenced: bool = False, actor: str = "owner"):
        if self.epoch is not None:
            raise Fenced('Demote this process before promoting it again')
        leader = self.control.connect()
        try:
            if self.postgres and not leader.execute(text('SELECT pg_try_advisory_lock(:key)'),
                                                     {'key': LEADER_LOCK}).scalar():
                raise Fenced('Another main still holds authority; demote it first')
            leader.commit()
            with self.control.begin() as connection:
                if self.postgres and not connection.execute(text('SELECT pg_try_advisory_xact_lock(:key)'),
                                                            {'key': ACTIVITY_LOCK}).scalar():
                    raise Fenced('Work is still in flight; drain the old main and retry')
                row = self._row(connection)
                if row['epoch'] != expected_epoch:
                    raise Fenced('Epoch changed; inspect status before retrying')
                if (row['holder'] is not None or row['uncertain']) and not old_main_fenced:
                    raise Fenced('The previous main must be independently fenced before crash recovery')
                result = connection.execute(text('UPDATE ha_authority SET epoch=epoch+1, holder=:holder, '
                                                  'incarnation=:incarnation, uncertain=FALSE WHERE id=1 AND epoch=:epoch'),
                                            {'holder': self.node_id, 'incarnation': self.incarnation,
                                             'epoch': expected_epoch})
                if result.rowcount != 1:
                    raise Fenced('Concurrent promotion refused')
                connection.execute(AuditLog.__table__.insert().values(
                    actor=actor, action='ha.promote', target=self.node_id,
                    detail={'epoch': expected_epoch + 1, 'previous_holder': row['holder'],
                            'old_main_fenced': old_main_fenced}))
            self._public = None
            self.leader = leader
            self.epoch = expected_epoch + 1
            self.uncertain = False
            self.accepting = True
        except BaseException:
            try:
                if self.postgres and not leader.closed and not leader.invalidated:
                    leader.rollback()
                    leader.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': LEADER_LOCK})
                    leader.commit()
            except SQLAlchemyError:
                leader.invalidate()
            finally:
                leader.close()
            raise

    def mark_uncertain(self, operation: str):
        """A sent effect has no trusted result. Never turn cancellation into drain."""
        with self.lock:
            if self.epoch is None:
                return
            self.uncertain = True  # demote refuses; this holder keeps its epoch
            self._public = None
            try:
                with self.control.begin() as connection:
                    result = connection.execute(text('UPDATE ha_authority SET uncertain=TRUE '
                                                      'WHERE id=1 AND epoch=:epoch AND incarnation=:incarnation'),
                                                {'epoch': self.epoch, 'incarnation': self.incarnation})
                    if result.rowcount:
                        connection.execute(AuditLog.__table__.insert().values(
                            actor='system', action='ha.command_uncertain', target=self.node_id,
                            detail={'epoch': self.epoch, 'operation': operation[:80]}))
            except SQLAlchemyError:
                # The local latch still refuses clean demotion. If the DB lost
                # its connections, its durable holder still requires crash fencing.
                pass

    def demote(self, *, expected_epoch: int, actor: str = "owner"):
        self.accepting = False
        with self.control.begin() as connection:
            if self.uncertain or self._row(connection)['uncertain']:
                raise Fenced('Command outcome is uncertain; independently fence the old main and resolve external work')
            if self.postgres and not connection.execute(text('SELECT pg_try_advisory_xact_lock(:key)'),
                                                        {'key': ACTIVITY_LOCK}).scalar():
                raise Fenced('Work is still draining; retry demotion')
            result = connection.execute(text('UPDATE ha_authority SET epoch=epoch+1, holder=NULL, incarnation=NULL '
                                              'WHERE id=1 AND epoch=:epoch AND incarnation=:incarnation AND uncertain=FALSE'),
                                        {'epoch': expected_epoch, 'incarnation': self.incarnation})
            if result.rowcount != 1:
                raise Fenced('This process does not hold that epoch')
            connection.execute(AuditLog.__table__.insert().values(
                actor=actor, action='ha.demote', target=self.node_id, detail={'epoch': expected_epoch + 1}))
        self.release()

    def release(self):
        with self.lock:
            self._public = None
            self.accepting = False
            self.epoch = None
            leader, self.leader = self.leader, None
            if leader is not None:
                try:
                    if self.postgres and not leader.closed:
                        leader.rollback()
                        leader.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': LEADER_LOCK})
                        leader.commit()
                except SQLAlchemyError:
                    leader.invalidate()
                finally:
                    leader.close()

    def install_write_guard(self):
        @event.listens_for(self.engine, 'before_cursor_execute')
        def guard(connection, cursor, statement, parameters, context, executemany):
            if context.execution_options.get('ha_check'):
                return
            # Includes Core, ORM bulk writes and raw SQL, not just Session.flush.
            if statement.lstrip().split(None, 1)[0].upper() in {'SELECT', 'PRAGMA', 'SHOW', 'SET', 'BEGIN'}:
                return
            if self.postgres:
                connection.execute(text('SELECT pg_advisory_xact_lock_shared(:key)'),
                                   {'key': ACTIVITY_LOCK}, execution_options={'ha_check': True})
            self.check()


def fence(db) -> None:
    """Re-check authority immediately before an effect beyond the database: a DNS
    change, a root-helper request or a node tool command. The last guarded SQL write
    may be arbitrarily old by then (a provider call, a helper wait)."""
    authority = (getattr(db, 'kw', {}).get('info') or {}).get('ha_authority')
    if authority is not None:
        authority.check()


@contextmanager
def schema_change(engine, ha_node_id: str):
    """Startup migrations on a shared database. A newer build must never change the
    schema under a main that holds authority; a database initialized for HA refuses
    a main without ACC_HA_NODE_ID before migrating anything."""
    if not inspect(engine).has_table('ha_authority'):
        yield  # new database or one that predates HA: the normal migration path
        return
    with engine.connect() as connection:
        rows = connection.execute(text('SELECT holder FROM ha_authority')).all()
        current = [r[0] for r in connection.execute(text('SELECT version_num FROM alembic_version'))]
        connection.rollback()
    if rows and not ha_node_id:
        raise ValueError('This database uses manual HA; configure ACC_HA_NODE_ID')
    if current == [ScriptDirectory(str(MIGRATIONS)).get_current_head()]:
        yield  # already current: startup changes nothing
        return
    lock = engine.connect() if engine.dialect.name == 'postgresql' else None
    try:
        if lock is not None:
            # Held through the migration: no main can be promoted meanwhile.
            if not lock.execute(text('SELECT pg_try_advisory_lock(:key)'), {'key': LEADER_LOCK}).scalar():
                raise ValueError('Another main holds authority; demote it before upgrading the shared schema')
            lock.commit()
            rows = lock.execute(text('SELECT holder FROM ha_authority')).all()
            lock.commit()
        holder = next((r[0] for r in rows if r[0]), None)
        if holder:
            raise ValueError(f'Main {holder} holds authority; demote it (or complete fenced recovery) '
                             'before upgrading the shared schema')
        yield
    finally:
        if lock is not None:
            try:
                lock.rollback()
                lock.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': LEADER_LOCK})
                lock.commit()
            except SQLAlchemyError:
                lock.invalidate()
            finally:
                lock.close()
