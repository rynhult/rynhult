"""Collector operator/privacy boundaries, using real SQLite and signed HTTP requests."""
import base64
import json
import sqlite3
import time
from datetime import datetime, timezone

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from acc import telemetry_collector as collector
from acc.telemetry_schema import encode, key_id


def signed(private, path, body):
    stamp = str(int(time.time()))
    return {
        'X-ACC-Time': stamp,
        'X-ACC-Key': base64.b64encode(private.public_key().public_bytes_raw()).decode(),
        'X-ACC-Signature': base64.b64encode(private.sign(stamp.encode() + b'\n' + path.encode() + b'\n' + body)).decode(),
    }


def seed(db, identity, kind, received, day='2026-09-01'):
    data = {'os': ['linux'], 'providers_enabled': ['codex'], 'features_enabled': []}
    db.execute('INSERT INTO payload VALUES (?,?,?,?,?,?)',
               (identity, identity, kind, day, json.dumps(data), received))


def test_monthly_totals_freeze_before_raw_expiry_and_small_cells_never_export(tmp_path):
    path = tmp_path / 'collector.db'
    collector.create_collector(path)
    now = datetime(2026, 10, 24, tzinfo=timezone.utc).timestamp()
    with collector.database(path) as db:
        for i in range(10):
            seed(db, str(i), 'usage', now, '2026-09-01')
        collector.retain(db, now)
        db.execute("DELETE FROM payload WHERE install='0'")
        collector.retain(db, now + 86400)
        assert json.loads(db.execute('SELECT totals FROM monthly').fetchone()[0])['os:linux'] == 10
        db.execute('INSERT INTO monthly VALUES (?,?)', ('2026-08', '{"os:linux":9,"os:windows":10}'))
        assert collector.export_statistics(db) == {
            '2026-08': {'os:windows': 10}, '2026-09': {'os:linux': 10, 'providers_enabled:codex': 10}}


def test_retention_exact_boundaries_and_calendar_months(tmp_path):
    collector.create_collector(tmp_path / 'db')
    now = datetime(2026, 10, 24, tzinfo=timezone.utc).timestamp()
    with collector.database(tmp_path / 'db') as db:
        for name, kind, age in [('u-expired', 'usage', 90 * 86400 + 1), ('u-keep', 'usage', 90 * 86400),
                                ('r-expired', 'reports', 180 * 86400 + 1), ('r-keep', 'reports', 180 * 86400)]:
            seed(db, name, kind, now - age)
        db.executemany('INSERT INTO monthly VALUES (?,?)', [('2025-08', '{}'), ('2025-09', '{}')])
        collector.retain(db, now)
        assert {r[0] for r in db.execute('SELECT id FROM payload')} == {'u-keep', 'r-keep'}
        assert '2025-08' not in {r[0] for r in db.execute('SELECT month FROM monthly')}
        assert '2025-09' in {r[0] for r in db.execute('SELECT month FROM monthly')}


async def test_key_limit_survives_ip_changes_deletion_and_spoofed_headers(tmp_path):
    app = collector.create_collector(tmp_path / 'db')
    private = Ed25519PrivateKey.generate()
    body = encode({'install': key_id(private.public_key().public_bytes_raw()), 'reports': []})
    for i in range(61):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(f'192.0.2.{i}', 1)),
                                     base_url='https://collector.example.com') as client:
            response = await client.post('/v1/delete', content=body, headers=signed(private, '/v1/delete', body))
        assert response.status_code == (200 if i < 60 else 429)


async def test_ip_limit_ignores_untrusted_forwarding_and_streaming_size_cap(tmp_path, caplog):
    app = collector.create_collector(tmp_path / 'db')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://collector.example.com') as c:
        async def chunks():
            yield b'x' * 3000
            yield b'x' * 3000
        assert (await c.post('/v1/usage', content=chunks())).status_code == 413
        for i in range(59):
            assert (await c.get('/v1/schemas', headers={'X-Forwarded-For': f'192.0.2.{i}'})).status_code == 200
        assert (await c.get('/v1/schemas')).status_code == 429
    assert '192.0.2.' not in caplog.text


async def test_rotated_keys_are_unlinked_and_old_key_can_erase_only_itself(tmp_path):
    path = tmp_path / 'db'
    app = collector.create_collector(path)
    old, new = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    ids = [key_id(k.public_key().public_bytes_raw()) for k in (old, new)]
    with collector.database(path) as db:
        for identity in ids:
            seed(db, identity, 'reports', time.time())
    body = encode({'install': ids[0], 'reports': []})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://collector.example.com') as c:
        assert (await c.post('/v1/delete', content=body, headers=signed(new, '/v1/delete', body))).status_code == 422
        assert (await c.post('/v1/delete', content=body, headers=signed(old, '/v1/delete', body))).status_code == 200
    with collector.database(path) as db:
        assert db.execute('SELECT install FROM payload').fetchall() == [(ids[1],)]


def test_operator_delete_and_backup_restore(tmp_path):
    from acc.telemetry_collector_ops import backup, delete_installation, restore
    path = tmp_path / 'db'
    collector.create_collector(path)
    with collector.database(path) as db:
        seed(db, 'a' * 22, 'reports', time.time())
        seed(db, 'b' * 22, 'reports', time.time())
    snapshot = tmp_path / 'backup'
    backup(path, snapshot)
    delete_installation(path, 'a' * 22)
    restored = tmp_path / 'restored'
    restore(snapshot, restored, ['a' * 22])
    with sqlite3.connect(restored) as db:
        assert db.execute('SELECT install FROM payload').fetchall() == [('b' * 22,)]
    assert snapshot.stat().st_mode & 0o077 == 0
    with pytest.raises(FileExistsError):
        backup(path, snapshot)


def test_monthly_finalization_waits_for_late_arrival_window(tmp_path):
    collector.create_collector(tmp_path / 'db')
    with collector.database(tmp_path / 'db') as db:
        seed(db, 'a', 'usage', datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp())
        collector.retain(db, datetime(2026, 10, 7, tzinfo=timezone.utc).timestamp())
        assert db.execute('SELECT count(*) FROM monthly').fetchone()[0] == 0
        collector.retain(db, datetime(2026, 10, 8, tzinfo=timezone.utc).timestamp())
        assert db.execute('SELECT count(*) FROM monthly').fetchone()[0] == 1


@pytest.mark.parametrize('endpoint,cap', [('/v1/usage', 4096), ('/v1/report', 16384), ('/v1/delete', 16384)])
async def test_endpoint_byte_limits(tmp_path, endpoint, cap):
    app = collector.create_collector(tmp_path / 'db')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://collector.example.com') as c:
        assert (await c.post(endpoint, content=b'x' * cap)).status_code == 401
        assert (await c.post(endpoint, content=b'x' * (cap + 1))).status_code == 413


async def test_closed_report_schema_and_daily_key_quota(tmp_path):
    from uuid import uuid4

    from acc.telemetry_schema import Report

    app = collector.create_collector(tmp_path / 'db')
    private = Ed25519PrivateKey.generate()
    report = Report(**{
        'schema': 'report/1', 'report': uuid4(), 'install': key_id(private.public_key().public_bytes_raw()),
        'acc': 'dev', 'os': 'linux', 'install_kind': 'git-checkout', 'trigger': 'server_error',
        'component': 'server', 'exception': 'ValueError', 'fingerprint': 'a' * 16,
        'frames': [{'module': 'acc/main.py', 'function': 'main', 'line': 1}],
    }).model_dump(mode='json', by_alias=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://collector.example.com') as c:
        for patch in [{'unknown': 'private'}, {'frames': [dict(report['frames'][0], unknown='private')]}]:
            body = encode(report | patch)
            assert (await c.post('/v1/report', content=body, headers=signed(private, '/v1/report', body))).status_code == 422
        for i in range(11):
            body = encode(report | {'report': str(uuid4())})
            response = await c.post('/v1/report', content=body, headers=signed(private, '/v1/report', body))
            assert response.status_code == (202 if i < 10 else 429)


def test_restore_refuses_missing_snapshot_without_creating_one(tmp_path):
    from acc.telemetry_collector_ops import restore

    missing = tmp_path / 'missing'
    with pytest.raises(FileNotFoundError):
        restore(missing, tmp_path / 'restored', [])
    assert not missing.exists()


@pytest.mark.parametrize("prefix", ["ACC", "RYNHULT"])
async def test_packaged_worker_over_unix_socket_without_request_logs(tmp_path, prefix):
    """Exercise the real deploy factory/Uvicorn command; the suite owns this worker."""
    import asyncio
    import os
    import shutil
    import subprocess
    import sys
    import tempfile
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    code = tmp_path / 'standalone'
    for package in ('acc', 'deploy', 'deploy/telemetry'):
        directory = code / package
        directory.mkdir(parents=True, exist_ok=True)
        (directory / '__init__.py').touch()
    for name in ('env.py', 'telemetry_collector.py', 'telemetry_schema.py', 'telemetry_collector_ops.py'):
        shutil.copyfile(root / 'src/acc' / name, code / 'acc' / name)
    shutil.copyfile(root / 'deploy/telemetry/app.py', code / 'deploy/telemetry/app.py')
    with tempfile.TemporaryDirectory(prefix='acc-col-') as directory:
        socket = Path(directory) / 'worker.sock'
        log = tmp_path / 'worker.log'
        with log.open('w') as output:
            process = subprocess.Popen([
                sys.executable, '-m', 'uvicorn', '--factory', 'deploy.telemetry.app:create_app',
                '--uds', str(socket), '--workers', '1', '--lifespan', 'on', '--no-access-log',
                '--log-config', str(root / 'deploy/telemetry/logging.json'),
                '--proxy-headers', '--forwarded-allow-ips=*', '--ws', 'none',
            ], cwd=code, env=os.environ | {'PYTHONPATH': str(code),
                                          f'{prefix}_COLLECTOR_DB': str(tmp_path / 'db')},
                stdout=output, stderr=output)
            try:
                async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=str(socket)),
                                             base_url='http://collector.example.com') as c:
                    for _ in range(100):
                        try:
                            response = await c.get('/v1/schemas')
                            break
                        except httpx.TransportError:
                            assert process.poll() is None, log.read_text()
                            await asyncio.sleep(0.05)
                    else:
                        pytest.fail('Worker did not become ready')
                    assert response.status_code == 200
                    # The protected socket is the proxy trust boundary: peer addresses
                    # must survive forwarding rather than sharing one global UDS quota.
                    for i in range(61):
                        limited = await c.get('/v1/schemas', headers={'X-Forwarded-For': '192.0.2.100'})
                        assert limited.status_code == (200 if i < 60 else 429)
                    assert (await c.get('/v1/schemas', headers={
                        'X-Forwarded-For': '192.0.2.101'})).status_code == 200
                    private = Ed25519PrivateKey.generate()
                    body = encode({'install': key_id(private.public_key().public_bytes_raw()), 'reports': []})
                    headers = signed(private, '/v1/delete', body) | {'X-Forwarded-For': '192.0.2.99'}
                    assert (await c.post('/v1/delete', content=body, headers=headers)).status_code == 200
                    assert (await c.post('/v1/usage?private-query-canary', content=b'private-body-canary')).status_code == 401
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        assert log.read_text() == ''


async def test_usage_quota_and_new_key_registration_limit(tmp_path):
    from datetime import timedelta

    from acc.telemetry_schema import Usage

    app = collector.create_collector(tmp_path / 'db')
    data = {
        'schema': 'usage/1', 'install': 'a' * 22, 'day': datetime.now(timezone.utc).date(),
        'acc': 'dev', 'os': ['linux'], 'install_kind': 'git-checkout', 'machines': '1', 'people': '1',
        'projects': '0', 'sessions_running': '0', 'vault_keys': '0', 'providers_enabled': [],
        'features_enabled': [], 'used': {}, 'by_provider': {},
    }
    private = Ed25519PrivateKey.generate()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://collector.example.com') as c:
        for days in range(3):
            body = encode(Usage(**(data | {'install': key_id(private.public_key().public_bytes_raw()),
                                           'day': data['day'] - timedelta(days=days)})))
            assert (await c.post('/v1/usage', content=body, headers=signed(private, '/v1/usage', body))).status_code == (
                202 if days < 2 else 429)
        for i in range(20):
            private = Ed25519PrivateKey.generate()
            body = encode(Usage(**(data | {'install': key_id(private.public_key().public_bytes_raw())})))
            response = await c.post('/v1/usage', content=body, headers=signed(private, '/v1/usage', body))
            assert response.status_code == (202 if i < 19 else 429)
