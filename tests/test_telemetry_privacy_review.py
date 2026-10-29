"""Telemetry privacy review (docs/security/2026-10-27-telemetry-privacy-review.md): one test group per fixed finding."""
import asyncio
import json

import httpx
from test_telemetry import telemetry  # noqa: F401

URL = "https://collector.example.com"


# TP-1: a tick held one transaction across every send, so a withdrawal made during a tick didn't stop its later sends
# (SQLite: the withdrawal waited for the whole tick; PostgreSQL: the tick read consent from its starting snapshot).
async def test_tp_1_withdrawal_during_a_tick_stops_its_later_sends(telemetry):  # noqa: F811
    from acc.server.models import TelemetryItem

    with telemetry.db() as s:
        telemetry.configure(s, True, False, URL, "owner")
        body = telemetry.usage(s)
        for name in ("first", "second"):
            s.add(TelemetryItem(id=name, kind="usage", key_id=json.loads(body)["install"], body=body,
                                state="approved", created=__import__("time").time(), due=0, attempts=0))
        s.commit()
    sent = []

    def withdraw():
        with telemetry.db() as w:
            telemetry.configure(w, False, False, URL, "owner")
            w.commit()

    async def receive(request):
        sent.append(request.content)
        if len(sent) == 1:  # the owner turns sharing off while the first item is on its way
            await asyncio.wait_for(asyncio.to_thread(withdraw), 10)
        return httpx.Response(202)

    telemetry.transport = httpx.MockTransport(receive)
    await asyncio.wait_for(telemetry.tick(), 30)
    assert len(sent) == 1
    with telemetry.db() as s:
        assert telemetry.consent(s)["usage"] is False
        assert s.query(TelemetryItem).filter(TelemetryItem.state != "sent").count() == 0


# TP-2: the notice promises a copy of what is held (and portability), but the collector's tools had no per-key export,
# and the runbook's single-report erasure had no command.
def collector_with_two_installations(tmp_path):
    import time

    from acc import telemetry_collector as collector
    path = tmp_path / 'collector.db'
    collector.create_collector(path)
    with collector.database(path) as db:
        for install, report in (('a' * 22, '11111111-1111-4111-8111-111111111111'),
                                ('a' * 22, '22222222-2222-4222-8222-222222222222'),
                                ('b' * 22, '33333333-3333-4333-8333-333333333333')):
            db.execute('INSERT INTO payload VALUES (?,?,?,?,?,?)',
                       (report, install, 'reports', '2026-10-27', json.dumps({'install': install}), time.time()))
        for install in ('a' * 22, 'b' * 22):
            db.execute('INSERT INTO signing_key VALUES (?,?,?,?)', (install, 'PUBLIC-' + install, 'reports', time.time()))
    return path


def ops(*args):
    import subprocess
    import sys
    return subprocess.run([sys.executable, '-m', 'acc.telemetry_collector_ops', *map(str, args)], capture_output=True,
                          text=True, env={**__import__('os').environ, 'PYTHONPATH': 'src'})


def test_tp_2_access_writes_only_that_key_ids_data_privately(tmp_path):
    path = collector_with_two_installations(tmp_path)
    out = tmp_path / 'access.json'
    result = ops('--db', path, 'access', 'a' * 22, out)
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text())
    assert out.stat().st_mode & 0o077 == 0
    assert {p['install'] for p in data['payloads']} == {'a' * 22} and len(data['payloads']) == 2
    assert [k['install'] for k in data['signing_key']] == ['a' * 22]
    assert 'b' * 22 not in out.read_text()
    assert ops('--db', path, 'access', 'a' * 22, out).returncode != 0  # never overwrites an earlier copy


def test_tp_2_one_report_can_be_erased_and_is_ledgered(tmp_path):
    import sqlite3
    path = collector_with_two_installations(tmp_path)
    result = ops('--db', path, 'delete-report', 'a' * 22, '11111111-1111-4111-8111-111111111111')
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(path) as db:
        assert sorted(r for (r,) in db.execute('SELECT id FROM payload')) == [
            '22222222-2222-4222-8222-222222222222', '33333333-3333-4333-8333-333333333333']
        assert db.execute('SELECT install, report FROM erasure').fetchall() == [
            ('a' * 22, '11111111-1111-4111-8111-111111111111')]


async def test_tp_1_a_withdrawal_through_the_api_waits_only_for_the_send_in_progress(telemetry):  # noqa: F811
    # The API's consent change takes send_lock; the tick held it across every send, so turning sharing off in the
    # dashboard waited for the whole queue to go out.
    import time

    from acc.server.models import TelemetryItem

    with telemetry.db() as s:
        telemetry.configure(s, True, False, URL, "owner")
        body = telemetry.usage(s)
        for name in ("first", "second", "third"):
            s.add(TelemetryItem(id=name, kind="usage", key_id=json.loads(body)["install"], body=body,
                                state="approved", created=time.time(), due=0, attempts=0))
        s.commit()
    sent, withdrawn = [], []

    async def owner_turns_sharing_off():  # as api/telemetry.py's PUT does it
        async with telemetry.send_lock:
            with telemetry.db() as w:
                telemetry.configure(w, False, False, URL, "owner")
                w.commit()
            withdrawn.append(len(sent))

    async def receive(request):
        sent.append(request.content)
        if len(sent) == 1:  # the owner's request arrives while the first item is on its way, and waits for the lock
            asyncio.get_running_loop().create_task(owner_turns_sharing_off())
            await asyncio.sleep(0)
        return httpx.Response(202)

    telemetry.transport = httpx.MockTransport(receive)
    await asyncio.wait_for(telemetry.tick(), 30)
    await asyncio.sleep(0)
    assert withdrawn == [1] and len(sent) == 1


# TP-3: the notice promises the staging data is deleted at the v1 launch; the runbook had no procedure for it.
def test_tp_3_the_runbook_has_the_v1_wipe_naming_every_place_data_is_kept():
    from pathlib import Path
    runbook = (Path(__file__).resolve().parents[1] / 'docs/TELEMETRY-COLLECTOR.md').read_text()
    section = runbook[runbook.index('## Version 1 launch: delete the staging data'):runbook.index('## Incident response')]
    for place in ('telemetry.sqlite', 'telemetry.sqlite-journal', 'backups/', 'erasures.txt', 'access-*.json',
                  'off-host backup', 'journalctl --vacuum-time', 'monthly totals', 'privacy notice'):
        assert place in section, place


# TP-6: a flood that filled the collector's rate-limit maps refused deletion requests too ("Busy").
def closure(function):
    return dict(zip(function.__code__.co_freevars, (cell.cell_contents for cell in function.__closure__)))


async def test_tp_6_a_full_rate_limit_map_never_refuses_a_deletion(tmp_path):
    import time

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from test_telemetry_collector_security import client, report, signed

    from acc import telemetry_collector as collector
    from acc.telemetry_schema import encode, key_id

    app = collector.create_collector(tmp_path / 'db')
    private = Ed25519PrivateKey.generate()
    body = encode(report(private))
    async with client(app, '192.0.2.10') as c:
        assert (await c.post('/v1/report', content=body, headers=signed(private, '/v1/report', body))).status_code == 202
    limits = next(m.kwargs['dispatch'] for m in app.user_middleware if 'dispatch' in m.kwargs)
    maps = closure(limits)
    route = next(r for r in app.routes if getattr(r, 'path', '') == '/v1/report')
    per_key = closure(closure(closure(route.endpoint)['collect'])['authenticated'])['key_windows']
    hour, day = int(time.time() // 3600), int(time.time() // 86400)
    for i in range(10000):  # a flood from many addresses and keys
        maps['windows'][(hour, f'flood-{i}')] = {'requests': 1}
        maps['registrations'][(day, f'flood-{i}')] = {'keys': 1}
        per_key[(hour, f'flood-{i}')] = 1
    other = Ed25519PrivateKey.generate()
    async with client(app, '198.51.100.7') as c:
        body = encode(report(other))
        assert (await c.post('/v1/report', content=body, headers=signed(other, '/v1/report', body))).status_code == 429
        erase = encode({'install': key_id(private.public_key().public_bytes_raw()), 'reports': []})
        response = await c.post('/v1/delete', content=erase, headers=signed(private, '/v1/delete', erase))
        assert response.status_code == 200, response.text
    with collector.database(tmp_path / 'db') as db:
        assert db.execute('SELECT count(*) FROM payload').fetchone()[0] == 0
