"""Security review of DNS providers phase 1 (SRD-01..07). Temporary roots only; no systemd, live Caddy or DNS API."""
import base64
import errno
import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest
from isolated_settings import isolated_settings
from sqlalchemy import select
from test_auth import PASSWORD
from test_dns_helper import NEW, TOKEN, apply, helper  # noqa: F401  (fixture)
from test_dns_infrastructure import CANARY, owner_client

from acc import backup_server as backup
from acc.server.db import migrate
from acc.server.vault import Cipher

REPO = Path(__file__).resolve().parents[1]


def unit(path):
    """systemd [Service] assignments in order, as (key, value) pairs."""
    return [tuple(part.strip() for part in line.split('=', 1)) for line in (REPO / path).read_text().splitlines()
            if '=' in line and not line.lstrip().startswith('#')]


# ---- SRD-01: the certificate token is persisted in Caddy's autosave ------------------------------------------------

def test_srd01_wildcard_config_carries_the_expanded_credential(tmp_path):
    """Mechanism (holds before and after the fix): {$CF_API_TOKEN} expands into the loaded JSON configuration."""
    caddy = shutil.which('caddy')
    if not caddy:
        pytest.skip('Caddy unavailable')
    text = (REPO / 'deploy/caddy/wildcard.caddy.in').read_text().replace('@ZONE@', 'example.com')
    text = text.replace('@ACC_HOST@', 'acc.example.com').replace('@PREVIEW_DOMAIN@', 'example.com')
    shutil.copy(REPO / 'deploy/caddy/acc-hardening.options', tmp_path / 'acc-hardening.options')
    (tmp_path / 'routes-example.com').mkdir()
    config = tmp_path / 'Caddyfile'
    config.write_text((REPO / 'deploy/caddy/Caddyfile').read_text().replace('import acc.d/*.caddy', '') + text)
    adapted = subprocess.run([caddy, 'adapt', '--config', str(config)], cwd=tmp_path, capture_output=True, text=True,
                             env={**os.environ, 'CF_API_TOKEN': CANARY, 'HOME': str(tmp_path)})
    assert adapted.returncode == 0, adapted.stderr
    assert CANARY in adapted.stdout
    assert json.loads(adapted.stdout)['admin']['config']['persist'] is False


def test_srd01_shipped_global_options_never_autosave_credentials(tmp_path):
    """Disposable Caddy run with the shipped global options; a stand-in site carries the expanded variable."""
    caddy = shutil.which('caddy')
    if not caddy:
        pytest.skip('Caddy unavailable')
    with socket.socket() as reserved:
        reserved.bind(('127.0.0.1', 0))
        port = reserved.getsockname()[1]
    shutil.copy(REPO / 'deploy/caddy/acc-hardening.options', tmp_path / 'acc-hardening.options')
    globals_ = (REPO / 'deploy/caddy/Caddyfile').read_text().replace('import acc.d/*.caddy', '')
    globals_ = re.sub(r'admin unix\S+', 'admin off', globals_)  # no socket outside the temporary root
    config = tmp_path / 'Caddyfile'
    config.write_text(globals_ + f'http://127.0.0.1:{port} {{\n\trespond "{{$CF_API_TOKEN}}"\n}}\n')
    home = tmp_path / 'home'
    process = subprocess.Popen([caddy, 'run', '--config', str(config)], cwd=tmp_path, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, env={**os.environ, 'CF_API_TOKEN': CANARY, 'HOME': str(home),
                                   'XDG_CONFIG_HOME': str(home / 'config'), 'XDG_DATA_HOME': str(home / 'data')})
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and process.poll() is None:
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=.2):
                    break
            except OSError:
                time.sleep(.05)
        assert process.poll() is None, 'temporary Caddy did not start'
        time.sleep(.3)
    finally:
        process.terminate()
        process.wait(timeout=10)
    leaked = [p for p in home.rglob('*') if p.is_file() and CANARY.encode() in p.read_bytes()]
    assert leaked == []


# ---- SRD-02: Publishing cannot take the shared lock inside its sandbox ----------------------------------------------

def load_publish(tmp_path, monkeypatch):
    monkeypatch.setenv('ACC_PUBLISH_ROOT', str(tmp_path / 'publish'))
    monkeypatch.setenv('ACC_CADDY_DIR', str(tmp_path / 'acc.d'))
    monkeypatch.setenv('ACC_WILDCARD_ZONE', 'example.com')
    monkeypatch.setenv('ACC_PUBLISH_ZONES', 'example.com')
    monkeypatch.setenv('ACC_ALLOWED_ORIGINS', 'https://acc.example.com')
    loader = importlib.machinery.SourceFileLoader('acc_publish_srd', str(REPO / 'deploy/publish/acc-publish'))
    spec = importlib.util.spec_from_loader('acc_publish_srd', loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    (tmp_path / 'publish/requests').mkdir(parents=True)
    (tmp_path / 'acc.d').mkdir()
    return module


def test_srd02_publishing_sandbox_can_open_the_shared_lock():
    writable = [v for k, v in unit('deploy/publish/acc-publish.service') if k == 'ReadWritePaths']
    paths = {p.lstrip('-') for line in writable for p in line.split()}
    assert '/run/acc-dns' in paths
    # The lock file exists before either side runs, so neither needs to create it under a read-only parent.
    assert 'f /run/acc-dns/caddy-config.lock 0600 root root -' in (REPO / 'deploy/dns/acc-dns.tmpfiles').read_text()


def test_srd02_unavailable_lock_refuses_requests_instead_of_crashing(tmp_path, monkeypatch):
    publish = load_publish(tmp_path, monkeypatch)
    request = {'id': 'req-0042', 'action': 'apply', 'host': 'lab.example.com', 'port': 8004, 'access': 'internal'}
    (publish.ROOT / 'requests/req-0042.json').write_text(json.dumps(request))
    real_open = os.open
    def read_only(path, flags, *args, **kwargs):
        if path in ('acc-dns', 'caddy-config.lock'):
            raise OSError(errno.EROFS, 'Read-only file system')
        return real_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(publish.os, 'open', read_only)
    publish.validate = publish.reload = lambda: pytest.fail('Caddy must not change without the shared lock')
    assert publish.main() == 0
    result = json.loads((publish.ROOT / 'results/req-0042.json').read_text())
    assert result['ok'] is False and 'lock' in result['detail']
    assert not (tmp_path / 'acc.d/routes-example.com/lab.example.com.caddy').exists()


def test_srd02_caddy_commands_under_the_lock_are_bounded(tmp_path, monkeypatch):
    publish = load_publish(tmp_path, monkeypatch)
    monkeypatch.setattr(publish, 'CADDY_ENV', tmp_path / 'absent.env')
    monkeypatch.setattr(publish, 'MANAGED_CADDY_ENV', tmp_path / 'absent/current.env')
    seen = []
    def run(argv, **kwargs):
        seen.append(kwargs)
        return subprocess.CompletedProcess(argv, 0, '', '')
    monkeypatch.setattr(publish.subprocess, 'run', run)
    publish.validate()
    publish.reload()
    assert all(0 < kwargs.get('timeout', 0) <= 60 for kwargs in seen)


# ---- SRD-03: a failed apply corrupts the rollback generation --------------------------------------------------------

def test_srd03_failed_apply_keeps_the_rollback_generation(helper):  # noqa: F811
    h, root, calls = helper
    first = apply(h)
    second = apply(h, NEW, 2)
    assert first['ok'] and second['ok']
    assert apply(h, 'bad-token', 3) == {'ok': False, 'code': 'apply_failed'}
    assert 'bad-token' not in ''.join(p.read_text() for p in root.iterdir() if p.is_file())
    restored = h.handle({'action': 'rollback'})
    assert restored == {'ok': True, 'generation': first['generation']}
    assert (root / 'current.env').read_text() == f'CF_API_TOKEN={TOKEN}\n'


def test_srd03_rollback_removes_the_abandoned_generation(helper):  # noqa: F811
    h, root, calls = helper
    assert apply(h)['ok'] and apply(h, NEW, 2)['ok']
    assert h.handle({'action': 'rollback'})['ok']
    assert NEW not in ''.join(p.read_text() for p in root.iterdir() if p.is_file())


# ---- SRD-04: apply holds the database write lock across the root helper call ------------------------------------

async def test_srd04_apply_releases_the_database_during_the_helper_call(api, env, monkeypatch):
    import sqlite3

    from acc.server import dns_helper_client
    headers = await owner_client(api, env)
    ids = []
    for purpose in ('certificates', 'publishing'):
        created = await api.post('/api/dns/credentials', headers=headers, json=dict(
            name=purpose, provider='cloudflare', purpose=purpose, fields={'api_token': CANARY}, password=PASSWORD))
        ids.append(created.json()['id'])
    saved = await api.post('/api/dns/binding', headers=headers, json=dict(provider='cloudflare', zone='example.com',
        certificate_id=ids[0], publishing_id=ids[1], password=PASSWORD))
    revision = saved.json()['revision']
    async def request(message):
        # The helper may take up to 30 seconds. Meanwhile another writer (here: the owner saving a new revision)
        # must not wait on accd's write lock.
        with sqlite3.connect(env['settings'].data_dir / 'acc.db', timeout=.5, isolation_level=None) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("update dns_binding set revision = revision + 1, status = 'saved'")
            db.execute('COMMIT')
        return {'ok': True, 'generation': 'g1', 'issuance': 'unverified'}
    monkeypatch.setattr(dns_helper_client, 'request', request)
    applied = await api.post('/api/dns/apply', headers=headers, json={'password': PASSWORD, 'revision': revision})
    assert applied.status_code == 200, applied.text
    current = (await api.get('/api/dns/binding')).json()
    # The newer revision was never applied, so it must not be reported as applied.
    assert current['revision'] == revision + 1 and current['applied_revision'] == revision
    assert current['status'] != 'applied'


# ---- SRD-05: the Caddy and helper sandboxes -----------------------------------------------------------------------

def test_srd05_caddy_sandbox_resets_merged_capabilities_and_filters_syscalls():
    pairs = unit('deploy/caddy/caddy.service.d-hardening.conf')
    for key in ('AmbientCapabilities', 'CapabilityBoundingSet'):
        values = [v for k, v in pairs if k == key]
        assert values == ['', 'CAP_NET_BIND_SERVICE'], key  # list settings merge with the distribution unit
    settings = dict(pairs)
    assert settings.get('SystemCallFilter') == '@system-service'
    assert settings.get('LimitCORE') == '0'
    for key in ('ProtectClock', 'ProtectHostname'):
        assert settings.get(key) == 'yes'
    assert settings.get('ProtectProc') == 'invisible'


def test_srd05_root_helper_has_a_bounded_capability_set():
    settings = dict(unit('deploy/dns/acc-dns.service'))
    caps = set(settings.get('CapabilityBoundingSet', 'all').split())
    assert caps == {'CAP_SETUID', 'CAP_SETGID', 'CAP_KILL'}
    assert settings.get('LimitCORE') == '0' and settings.get('SystemCallFilter') == '@system-service'
    assert dict(unit('deploy/dns/acc-dns-purge.service')).get('CapabilityBoundingSet') == ''


def test_srd05_units_parse_without_unknown_settings(tmp_path):
    """systemd-analyze verify flags unknown or malformed keys; ExecStart is replaced by a harmless binary."""
    analyze = shutil.which('systemd-analyze')
    if not analyze:
        pytest.skip('systemd-analyze unavailable')
    units = []
    for source in ('deploy/dns/acc-dns.service', 'deploy/dns/acc-dns-purge.service', 'deploy/publish/acc-publish.service'):
        text = re.sub(r'(?m)^ExecStart=.*$', 'ExecStart=/bin/true', (REPO / source).read_text())
        units.append(tmp_path / Path(source).name)
        units[-1].write_text(text)
    shutil.copy(REPO / 'deploy/dns/acc-dns.socket', tmp_path / 'acc-dns.socket')
    units.append(tmp_path / 'caddy-hardening.service')
    units[-1].write_text('[Unit]\nDescription=drop-in check\n' + (REPO / 'deploy/caddy/caddy.service.d-hardening.conf')
                         .read_text() + 'ExecStart=/bin/true\n')
    result = subprocess.run([analyze, 'verify', '--man=no', *map(str, units)], capture_output=True, text=True,
                            cwd=tmp_path)
    assert result.returncode == 0 and 'Unknown' not in result.stderr, result.stderr


# ---- SRD-06: refused privileged attempts leave no audit record ----------------------------------------------------

async def test_srd06_refused_reauthentication_and_unavailable_helper_are_audited(api, env, monkeypatch):
    from acc.server import dns_helper_client
    from acc.server.models import AuditLog
    headers = await owner_client(api, env)
    ids = []
    for purpose in ('certificates', 'publishing'):
        created = await api.post('/api/dns/credentials', headers=headers, json=dict(
            name=purpose, provider='cloudflare', purpose=purpose, fields={'api_token': CANARY}, password=PASSWORD))
        ids.append(created.json()['id'])
    saved = await api.post('/api/dns/binding', headers=headers, json=dict(provider='cloudflare', zone='example.com',
        certificate_id=ids[0], publishing_id=ids[1], password=PASSWORD))
    wrong = await api.post('/api/dns/apply', headers=headers, json={'password': 'not-the-password', 'revision': 1})
    assert wrong.status_code == 403
    async def unavailable(message):
        raise OSError('helper absent')
    monkeypatch.setattr(dns_helper_client, 'request', unavailable)
    for path, body in [('/api/dns/apply', {'revision': saved.json()['revision']}), ('/api/dns/rollback', {}),
                       ('/api/dns/migrate', {})]:
        assert (await api.post(path, headers=headers, json={'password': PASSWORD, **body})).status_code == 409
    with api.app.state.db() as s:
        audits = s.scalars(select(AuditLog).where(AuditLog.action.like('dns.%'))).all()
    outcomes = {(a.action, (a.detail or {}).get('result')) for a in audits}
    assert ('dns.reauth', 'refused') in outcomes
    assert {('dns.apply', 'unavailable'), ('dns.rollback', 'unavailable'), ('dns.migrate', 'refused')} <= outcomes
    assert 'not-the-password' not in json.dumps([a.detail for a in audits]) and CANARY not in json.dumps(
        [a.detail for a in audits])


# ---- SRD-07: backup pairing ignores infrastructure ciphertext ------------------------------------------------------

def test_srd07_restore_refuses_infrastructure_values_from_another_key(tmp_path):
    from acc.server import infrastructure as infra
    from acc.server.db import make_engine, make_sessionmaker
    from acc.server.models import InfrastructureCredential
    root = tmp_path / 'main'
    settings = isolated_settings(root)
    settings.data_dir.mkdir(parents=True)
    migrate(settings.db_url)
    cipher = Cipher(settings.vault_key_path)
    cipher.key(create=True)
    settings.catalog_local_dir.mkdir()
    (settings.catalog_local_dir / 'local.json').write_text('{}')
    service = backup.Service(settings)
    service.initialize()
    key = InfrastructureCredential(id='b' * 16, name='Cert', provider='cloudflare', purpose='certificates',
                                   owner='owner', revision=1)
    key.ciphertext = cipher.seal(key.id, infra.aad(key), CANARY)
    with make_sessionmaker(make_engine(settings.db_url))() as db:
        db.add(key)
        db.commit()
    stage = service.capture()
    (stage / 'secrets/vault.key').write_text(base64.b64encode(os.urandom(32)).decode())
    m = json.loads((stage / 'manifest.json').read_text())['manifest']
    m['vault_key_epoch'] = backup.digest(stage / 'secrets/vault.key')
    m['files'] = backup.inventory(stage)
    service.sign(stage, m)
    with pytest.raises(backup.Refused, match='pairing'):
        backup.validate_and_install(stage, service.kit(), stage.name, tmp_path / 'replacement')


# ---- Notes: robustness of the helper loop and route registration ----------------------------------------------------

def test_srd_note_helper_survives_hostile_nesting_from_its_peer(helper):  # noqa: F811
    from acc.dns_helper import respond
    h, root, calls = helper
    reply = respond(b'[' * 15000 + b'\n', h, None)
    assert json.loads(reply) == {'ok': False, 'code': 'refused'} and calls == []


async def test_srd_note_dns_routes_are_registered_once(api):
    from test_ownership import all_routes
    routes = [(prefix + r.path, tuple(sorted(r.methods))) for prefix, r in all_routes(api.app.routes)
              if (prefix + getattr(r, 'path', '')).startswith('/api/dns/')]
    assert routes and len(routes) == len(set(routes))
