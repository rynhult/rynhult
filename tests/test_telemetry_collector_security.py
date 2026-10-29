"""Security review 2026-10-24 of the Internet-facing collector: one test per confirmed finding."""
import base64
import json
import re
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from acc import telemetry_collector as collector
from acc.telemetry_schema import Report, encode, key_id

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / 'deploy/telemetry'


def signed(private, path, body):
    stamp = str(int(time.time()))
    return {
        'X-ACC-Time': stamp,
        'X-ACC-Key': base64.b64encode(private.public_key().public_bytes_raw()).decode(),
        'X-ACC-Signature': base64.b64encode(private.sign(stamp.encode() + b'\n' + path.encode() + b'\n' + body)).decode(),
    }


def report(private, **fields):
    return Report(**{
        'schema': 'report/1', 'report': uuid4(), 'install': key_id(private.public_key().public_bytes_raw()),
        'acc': 'dev', 'os': 'linux', 'install_kind': 'git-checkout', 'trigger': 'server_error',
        'component': 'server', 'exception': 'ValueError', 'fingerprint': 'a' * 16,
        'frames': [{'module': 'acc/main.py', 'function': 'main', 'line': 1}],
    } | fields)


def client(app, address='192.0.2.1'):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(address, 1)),
                             base_url='https://collector.example.com')


# High: the collector stored report free text exactly as an (untrusted) client sent it.
@pytest.mark.parametrize('message', [
    'failed for alice@corp.example.com from 203.0.113.77 password=hunter2-pass',
    'peer 2001:db8::7 used ghp_' + 'A' * 30,
    'open /home/alice/secret-project failed',
    'fetch https://bob:pw@git.example.com/x',
    'jwt eyJhbGciOi.eyJzdWIiOi.c2lnbmF0dXJlMTIz',
    'blob ' + 'Q' * 40,
    '\x1b[31mred‮',
])
async def test_collector_scrubs_report_text_instead_of_trusting_clients(tmp_path, message):
    path = tmp_path / 'db'
    app = collector.create_collector(path)
    private = Ed25519PrivateKey.generate()
    canaries = ['alice@corp.example.com', '203.0.113.77', '2001:db8::7', 'ghp_' + 'A' * 30, 'hunter2-pass',
                '/home/alice/secret-project', 'https://bob:pw@git.example.com/x', 'eyJhbGciOi.eyJzdWIiOi.c2lnbmF0dXJlMTIz',
                'Q' * 40, '\x1b[31m', '‮']
    value = report(private, message=message)
    body = encode(value)
    async with client(app) as c:
        assert (await c.post('/v1/report', content=body, headers=signed(private, '/v1/report', body))).status_code == 202
    with collector.database(path) as db:
        stored = db.execute('SELECT body FROM payload').fetchone()[0]
    for canary in canaries:
        assert canary not in stored, canary
    assert Report.model_validate_json(stored).report == value.report


def test_server_scrub_keeps_client_scrubbed_text_unchanged():
    # The design's example output of the client scrubber is a fixpoint: compliant clients lose nothing.
    for text in ['broker call providers.check timed out after 30 s on <machine>',
                 'WARNING acc.server.supervisor: providers: <machine> did not answer',
                 'ERROR acc.server.supervisor: supervisor stage providers failed',
                 'open <path>', 'sent «secret» to <email> at <ip> via <url>', '']:
        assert collector.scrub_text(text) == text


# Medium: self-service erasures left no record, so any backup restore resurrected erased data.
async def test_self_service_erasures_are_ledgered_and_replayed_on_restore(tmp_path):
    from acc.telemetry_collector_ops import backup, erasures, restore

    path = tmp_path / 'db'
    app = collector.create_collector(path)
    gone, partial = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    kept = report(partial)
    erased_report = report(partial)
    async with client(app) as c:
        for key, value in [(gone, report(gone)), (partial, kept), (partial, erased_report)]:
            body = encode(value)
            assert (await c.post('/v1/report', content=body, headers=signed(key, '/v1/report', body))).status_code == 202
    snapshot = tmp_path / 'snapshot'
    backup(path, snapshot)
    async with client(app) as c:
        for key, reports in [(gone, []), (partial, [str(erased_report.report)])]:
            body = encode({'install': key_id(key.public_key().public_bytes_raw()), 'reports': reports})
            assert (await c.post('/v1/delete', content=body, headers=signed(key, '/v1/delete', body))).status_code == 200
    ledger = erasures(path)
    restored = tmp_path / 'restored'
    restore(snapshot, restored, ledger)
    with sqlite3.connect(restored) as db:
        assert [r[0] for r in db.execute('SELECT id FROM payload')] == [str(kept.report)]
    # The ledger holds key ids/report ids only, and expires after backups can no longer exist.
    with collector.database(path) as db:
        assert db.execute('SELECT count(*) FROM erasure').fetchone()[0] == 2
        collector.retain(db, time.time() + 46 * 86400)
        assert db.execute('SELECT count(*) FROM erasure').fetchone()[0] == 0


# Medium: no global storage bound; many keys/addresses could fill the disk.
async def test_global_storage_cap_refuses_new_data_but_still_deletes(tmp_path):
    path = tmp_path / 'db'
    app = collector.create_collector(path, max_bytes=1)
    private = Ed25519PrivateKey.generate()
    body = encode(report(private))
    async with client(app) as c:
        assert (await c.post('/v1/report', content=body, headers=signed(private, '/v1/report', body))).status_code == 429
        body = encode({'install': key_id(private.public_key().public_bytes_raw()), 'reports': []})
        assert (await c.post('/v1/delete', content=body, headers=signed(private, '/v1/delete', body))).status_code == 200
    with collector.database(path) as db:
        assert db.execute('SELECT count(*) FROM payload').fetchone()[0] == 0


# Medium: per-address limits keyed on full IPv6 addresses; one /64 holds 2^64 of them.
async def test_ipv6_addresses_share_limits_per_64_prefix(tmp_path):
    app = collector.create_collector(tmp_path / 'db')
    for i in range(61):
        async with client(app, f'2001:db8:1:2::{i:x}:1') as c:
            assert (await c.get('/v1/schemas')).status_code == (200 if i < 60 else 429)
    async with client(app, '2001:db8:1:3::1') as c:
        assert (await c.get('/v1/schemas')).status_code == 200


# Medium: dependencies installed as root without hashes; transitive packages unpinned; sdists allowed.
def test_installer_requires_hash_locked_wheels():
    requirements = (DEPLOY / 'requirements.txt').read_text()
    entries = re.findall(r'^([A-Za-z0-9_.-]+)==\S+', requirements, re.M)
    assert {'fastapi', 'uvicorn', 'pydantic', 'cryptography', 'starlette', 'pydantic-core', 'cffi'} <= set(entries)
    for entry in re.split(r'\n(?=[A-Za-z])', requirements.split('\n', 1)[1].strip()):
        assert '--hash=sha256:' in entry, entry
    script = (DEPLOY / 'install.sh').read_text()
    for flag in ('--require-hashes', '--only-binary :all:', '--no-deps'):
        assert flag in script
    assert 'curl' not in script and 'wget' not in script


# Low: the Internet-facing worker ran with Caddy's group; sandbox lacked several standard restrictions.
def test_service_has_own_group_and_full_sandbox():
    unit = (DEPLOY / 'acc-telemetry.service').read_text()
    assert 'Group=acc-telemetry' in unit and 'Group=caddy' not in unit
    for line in ('IPAddressDeny=any', 'RestrictNamespaces=true', 'ProtectKernelLogs=true', 'ProtectClock=true',
                 'ProtectHostname=true', 'ProtectProc=invisible', 'ProcSubset=pid', 'RestrictRealtime=true',
                 'SystemCallArchitectures=native', 'SystemCallFilter=@system-service', 'RemoveIPC=true',
                 'PrivateUsers=true', 'DevicePolicy=closed', 'KeyringMode=private'):
        assert line in unit.splitlines(), line
    assert 'SupplementaryGroups=acc-telemetry' in (DEPLOY / 'caddy-limits.conf').read_text()
    for text in ((DEPLOY / 'install.sh').read_text(), (ROOT / 'docs/TELEMETRY-COLLECTOR.md').read_text()):
        assert '-g caddy' not in text


@pytest.mark.skipif(not shutil.which('systemd-analyze'), reason='needs systemd-analyze')
def test_service_unit_parses_without_unknown_directives(tmp_path):
    unit = tmp_path / 'acc-telemetry.service'
    unit.write_text((DEPLOY / 'acc-telemetry.service').read_text().replace(
        '/opt/acc-telemetry/venv/bin/python', shutil.which('python3') or '/usr/bin/python3'))
    result = subprocess.run(['systemd-analyze', 'verify', str(unit)], capture_output=True, text=True)
    assert 'Unknown key' not in result.stderr and 'Unknown lvalue' not in result.stderr, result.stderr


# Low: no HSTS/nosniff/referrer headers; the static root served dotfiles.
@pytest.mark.skipif(not shutil.which('caddy'), reason='Caddy is not installed')
def test_caddy_sends_security_headers_and_hides_dotfiles():
    result = subprocess.run(['caddy', 'adapt', '--config', str(DEPLOY / 'Caddyfile'), '--adapter', 'caddyfile'],
                            check=True, capture_output=True, text=True)
    config = json.dumps(json.loads(result.stdout))
    for needle in ('Strict-Transport-Security', 'X-Content-Type-Options', 'Referrer-Policy',
                   'Content-Security-Policy', '"Server"', '".*"'):
        assert needle in config, needle


# Low: runbook left decrypted snapshots and erasure lists in the state directory indefinitely.
def test_runbook_removes_plaintext_snapshots_and_exports_ledger():
    runbook = (ROOT / 'docs/TELEMETRY-COLLECTOR.md').read_text()
    assert 'rm /var/lib/acc-telemetry/backups/recovered.sqlite' in runbook
    assert 'rm /var/lib/acc-telemetry/erasures.txt' in runbook
    assert 'erasures' in runbook and 'telemetry.sqlite erasures' in runbook


@pytest.mark.parametrize('extra', [{'build': 'a' * 40}, {'build': 'dev'}, {'log': []}, {'log': ['private-canary']}])
async def test_sr10_4_collector_refuses_undisclosed_report_fields(tmp_path, extra):
    private = Ed25519PrivateKey.generate()
    payload = report(private).model_dump(mode='json', by_alias=True)
    payload.pop('build', None)
    payload.pop('log', None)
    path = tmp_path / 'db'
    async with client(collector.create_collector(path)) as c:
        body = encode(payload)
        assert (await c.post('/v1/report', content=body, headers=signed(private, '/v1/report', body))).status_code == 202
        body = encode(payload | extra)
        assert (await c.post('/v1/report', content=body, headers=signed(private, '/v1/report', body))).status_code == 422
        schema = (await c.get('/v1/schemas')).json()['report']
        assert schema['additionalProperties'] is False
        assert not {'build', 'log'} & schema['properties'].keys()
    with collector.database(path) as db:
        assert db.execute('SELECT count(*) FROM payload').fetchone()[0] == 1
