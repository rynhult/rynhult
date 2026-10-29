"""Offline collector administration; run as its service user, never an HTTP admin API."""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from uuid import UUID

from .telemetry_collector import create_collector, database, export_statistics, record_erasure, retain


def delete_installation(path: Path, installation: str):
    if not re.fullmatch(r'[A-Za-z0-9_-]{22}', installation):
        raise ValueError('Invalid installation key id')
    with database(path) as db:
        db.execute('PRAGMA secure_delete=ON')
        db.execute('DELETE FROM payload WHERE install=?', (installation,))
        db.execute('DELETE FROM signing_key WHERE install=?', (installation,))
        record_erasure(db, installation)


def delete_report(path: Path, installation: str, report: str):
    if not re.fullmatch(r'[A-Za-z0-9_-]{22}', installation):
        raise ValueError('Invalid installation key id')
    report = str(UUID(report))
    with database(path) as db:
        db.execute('PRAGMA secure_delete=ON')
        if db.execute('DELETE FROM payload WHERE install=? AND id=?', (installation, report)).rowcount:
            record_erasure(db, installation, report)


def access(path: Path, installation: str) -> dict:
    """Everything held for one key id, for a data-subject access or portability request (telemetry privacy review,
    TP-2): its registration, every payload and its erasure records. Monthly totals hold no key ids."""
    if not re.fullmatch(r'[A-Za-z0-9_-]{22}', installation):
        raise ValueError('Invalid installation key id')
    with database(path) as db:
        db.row_factory = sqlite3.Row
        return {
            'key_id': installation,
            'signing_key': [dict(row) for row in db.execute('SELECT * FROM signing_key WHERE install=?', (installation,))],
            'payloads': [{**dict(row), 'body': json.loads(row['body'])} for row in
                         db.execute('SELECT * FROM payload WHERE install=? ORDER BY received', (installation,))],
            'erasures': [dict(row) for row in db.execute('SELECT * FROM erasure WHERE install=? ORDER BY erased',
                                                         (installation,))],
        }


def write_access(path: Path, installation: str, destination: Path):
    data = access(path, installation)
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as output:
        json.dump(data, output, indent=2, sort_keys=True)
        output.write('\n')


def erasures(path: Path) -> list[str]:
    """Ledger lines, `KEY_ID` or `KEY_ID REPORT_UUID`, for erasures within the ledger window."""
    with database(path) as db:
        return [f'{install} {report}'.strip()
                for install, report in db.execute('SELECT install,report FROM erasure ORDER BY erased')]


def write_erasures(path: Path, destination: Path):
    lines = erasures(path)
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as output:
        output.write(''.join(line + '\n' for line in lines))


def backup(path: Path, destination: Path):
    # Exclusive creation prevents overwriting a snapshot or following a symlink.
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        with database(path) as source:
            with sqlite3.connect(destination) as target:
                source.backup(target)
    except BaseException:
        destination.unlink()
        raise


def restore(snapshot: Path, destination: Path, deleted: list[str]):
    # Source and destination both receive the normal storage security checks.
    if not snapshot.is_file():
        raise FileNotFoundError(snapshot)
    create_collector(snapshot)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    backup(snapshot, destination)
    create_collector(destination)
    for line in deleted:
        installation, _, report = line.strip().partition(' ')
        if report:
            delete_report(destination, installation, report)
        else:
            delete_installation(destination, installation)
    with database(destination) as db:
        db.execute('PRAGMA secure_delete=ON')
        retain(db, time.time())
        if db.execute('PRAGMA integrity_check').fetchone() != ('ok',):
            raise ValueError('Restore integrity check failed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('retain')
    commands.add_parser('export')
    ledger = commands.add_parser('erasures', help='Write the erasure ledger for replay on restore')
    ledger.add_argument('destination', type=Path)
    delete = commands.add_parser('delete')
    delete.add_argument('installation')
    one = commands.add_parser('delete-report', help='Erase one report of a key id (recorded in the erasure ledger)')
    one.add_argument('installation')
    one.add_argument('report')
    copy = commands.add_parser('access', help='Write everything held for a key id to a new private JSON file')
    copy.add_argument('installation')
    copy.add_argument('destination', type=Path)
    snap = commands.add_parser('backup')
    snap.add_argument('destination', type=Path)
    load = commands.add_parser('restore')
    load.add_argument('snapshot', type=Path)
    load.add_argument('--deletions', type=Path, required=True, help='Private file: erasure ledger lines (KEY_ID or KEY_ID REPORT_UUID)')
    args = parser.parse_args()
    if args.command == 'restore':
        restore(args.snapshot, args.db, args.deletions.read_text().splitlines())
        return
    if not args.db.is_file():
        parser.error('Existing database required')
    create_collector(args.db)
    if args.command == 'delete':
        delete_installation(args.db, args.installation)
    elif args.command == 'delete-report':
        delete_report(args.db, args.installation, args.report)
    elif args.command == 'access':
        write_access(args.db, args.installation, args.destination)
    elif args.command == 'backup':
        backup(args.db, args.destination)
    elif args.command == 'erasures':
        write_erasures(args.db, args.destination)
    else:
        with database(args.db) as db:
            db.execute('PRAGMA secure_delete=ON')
            retain(db, time.time())
            if args.command == 'export':
                print(json.dumps(export_statistics(db), sort_keys=True))


if __name__ == '__main__':
    main()
