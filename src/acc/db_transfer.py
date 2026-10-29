"""Offline, owner-run SQLite → PostgreSQL transfer. Never opens SQLite for writing."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import JSON, Integer, MetaData, String, Text, cast, create_engine, func, inspect, null, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from acc.env import environment

from .server.db import make_engine, migrate


@contextmanager
def open_source(path: Path):
    path = path.resolve(strict=True)
    engine = create_engine('sqlite://', creator=lambda: sqlite3.connect(path.as_uri() + '?mode=ro', uri=True))
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql('PRAGMA query_only=ON')
            connection.exec_driver_sql('BEGIN')  # one consistent read snapshot for copy and verification
            try:
                yield connection
            finally:
                connection.rollback()
    finally:
        engine.dispose()


def _tables(connection):
    names = inspect(connection).get_table_names()
    shadows = ({'transcript_fts_' + suffix for suffix in ('data', 'idx', 'content', 'docsize', 'config')}
               if connection.dialect.name == 'sqlite' else set())
    metadata = MetaData()
    metadata.reflect(connection, only=[name for name in names if name not in shadows])
    if 'transcript_fts' in metadata.tables:
        table = metadata.tables['transcript_fts']
        for column in table.columns:
            column.type = Integer() if column.name == 'seq' else String(32) if column.name == 'session_id' else Text()
    return metadata


def _rows(connection, table):
    # Keep SQL NULL distinct from JSON null, and canonicalize JSON independently of spacing/key order.
    columns = [cast(c, Text).label(c.name) if isinstance(c.type, JSON) else c for c in table.columns]
    for row in connection.execute(select(*columns)).mappings():
        values = dict(row)
        if table.name == 'transcript_fts':
            for key in ('text', 'session_id', 'role', 'ts'):
                if values[key] is not None:
                    values[key] = str(values[key])
        yield values


def _fingerprint(connection, table):
    hashes = []
    for row in _rows(connection, table):
        values = []
        for column in table.columns:
            value = row[column.name]
            if isinstance(column.type, JSON) and value is not None:
                value = {'json': json.loads(value)}
            elif isinstance(value, datetime):
                value = value.isoformat(timespec='microseconds')
            elif isinstance(value, date):
                value = value.isoformat()
            elif isinstance(value, bytes):
                value = {'bytes': value.hex()}
            values.append([column.name, value])
        encoded = json.dumps(values, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
        hashes.append(hashlib.sha256(encoded).digest())
    checksum = hashlib.sha256(b''.join(sorted(hashes))).hexdigest()
    return {'rows': len(hashes), 'sha256': checksum}


def verify_database(source, target):
    src, dst = _tables(source), _tables(target)
    if set(src.tables) != set(dst.tables):
        raise ValueError('source and target tables differ')
    report = {}
    for name, table in src.tables.items():
        if list(table.columns.keys()) != list(dst.tables[name].columns.keys()):
            raise ValueError(f'columns differ: {name}')
        expected = _fingerprint(source, table)
        if expected != _fingerprint(target, dst.tables[name]):
            raise ValueError(f'count/checksum mismatch: {name}')
        report[name] = expected
    return report


def copy_database(source, target):
    src, dst = _tables(source), _tables(target)
    if set(src.tables) != set(dst.tables):
        raise ValueError('source and target tables differ; use the same Rynhult schema version')
    if target.dialect.name == 'postgresql':
        names = ', '.join(target.dialect.identifier_preparer.quote(name) for name in dst.tables)
        target.exec_driver_sql(f'LOCK TABLE {names} IN ACCESS EXCLUSIVE MODE')
    for name, table in dst.tables.items():
        if name != 'alembic_version' and target.scalar(select(func.count()).select_from(table)):
            raise ValueError('target must be empty; no rows were copied')
    if source.scalar(text('SELECT version_num FROM alembic_version')) != target.scalar(text('SELECT version_num FROM alembic_version')):
        raise ValueError('source and target schema versions differ')
    for table in src.sorted_tables:
        if table.name == 'alembic_version':
            continue
        target_table = dst.tables[table.name]
        batch = []
        for row in _rows(source, table):
            for column in table.columns:
                if isinstance(column.type, JSON):
                    raw = row[column.name]
                    value = json.loads(raw) if raw is not None else None
                    row[column.name] = null() if raw is None else JSON.NULL if value is None else value
            batch.append(row)
            if len(batch) == 500:
                target.execute(target_table.insert(), batch)
                batch.clear()
        if batch:
            target.execute(target_table.insert(), batch)
    report = verify_database(source, target)  # mismatch raises before the enclosing transaction commits
    if target.dialect.name == 'postgresql':
        for table in dst.tables.values():
            for column in table.primary_key.columns:
                if not isinstance(column.type, Integer):
                    continue
                quoted = target.dialect.identifier_preparer.quote(table.name)
                sequence = target.scalar(text('SELECT pg_get_serial_sequence(:table, :column)'),
                                         {'table': quoted, 'column': column.name})
                if sequence:
                    maximum = target.scalar(select(func.max(column)).select_from(table))
                    target.execute(text('SELECT setval(CAST(:sequence AS regclass), :value, :called)'),
                                   {'sequence': sequence, 'value': max(maximum or 1, 1), 'called': maximum is not None})
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sqlite', type=Path, required=True, help='stopped Rynhult SQLite database (read only)')
    parser.add_argument('--target-env', default='ACC_DATABASE_URL', help='environment variable containing PostgreSQL URL')
    parser.add_argument('--verify-only', action='store_true')
    args = parser.parse_args()
    url = environment.get(args.target_env, '')
    try:
        backend = make_url(url).get_backend_name() if url else None
    except (ArgumentError, ValueError):
        backend = None
    if backend != 'postgresql':
        parser.error('target environment variable must contain a PostgreSQL URL')
    if os.getuid() != 0 or os.geteuid() != 0:
        parser.error('transfer requires root; run it offline from an owner-controlled root shell')
    engine = None
    try:
        with open_source(args.sqlite) as source:
            engine = make_engine(url)
            with engine.connect() as target:
                empty_schema = not inspect(target).get_table_names()
            if empty_schema and not args.verify_only:
                migrate(url)
            with engine.begin() as target:
                report = verify_database(source, target) if args.verify_only else copy_database(source, target)
            print(json.dumps({'verified': True, 'tables': report}, indent=2, sort_keys=True))
    except ImportError:
        parser.exit(2, "PostgreSQL transfer requires the postgres Python extra (psycopg).\n")
    except Exception:
        # DBAPI errors can include credentials or row values. Never print them in an owner report.
        parser.exit(1, 'Transfer refused or verification failed; target transaction rolled back. Check schema/version and empty target.\n')
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == '__main__':
    main()
