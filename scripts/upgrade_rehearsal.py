#!/usr/bin/env python3
"""Upgrade rehearsal: synthetic data at schema 0066, upgraded by the app itself in the e2e server, then checked.

    PYTHONPATH=src:tests .venv/bin/python scripts/upgrade_rehearsal.py --work DIR --port PORT [--postgres BIN]

Steps (each one's result goes to DIR/result.json):
1. a new database (SQLite in DIR, or a throwaway PostgreSQL cluster from BIN, Unix socket only) migrated to 0066 and
   filled by tests/upgrade_rehearsal.py: people, projects, sessions with history, devices, a group, backups, DNS
   credentials, telemetry consent. All of it is invented; nothing is read from a live instance;
2. tests/e2e_server.py started on that data (its own startup migration does the upgrade), signed in to as the
   synthetic owner, and asked for the seeded data through the API; then stopped by its PID;
3. every 0066 column of every table compared before and after; rows only the new schema holds added;
4. a downgrade to 0066, compared again.
The PostgreSQL cluster is stopped and its folder (inside DIR) removed at the end. Never point this at real data.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO / 'src'), str(REPO / 'tests')]

import upgrade_rehearsal as rehearsal  # noqa: E402
from upgrade_rehearsal import Cluster, short_folder  # noqa: E402

from acc.server import auth  # noqa: E402


def probe(work: Path, port: int, url: str, data: Path, expected: dict) -> dict:
    """Start the e2e server on the seeded data, sign in as the synthetic owner and read the seeded data back."""
    base = short_folder(work, 'rhe2e.')
    (work / 'e2e-base').write_text(str(base))
    env = {**os.environ, 'PYTHONPATH': f"{REPO / 'src'}:{REPO / 'tests'}", 'ACC_E2E_SEED_DATA': str(data),
           'ACC_E2E_DATABASE_URL': url if url.startswith('postgresql') else ''}
    log = (work / 'e2e.log').open('w')
    server = subprocess.Popen([sys.executable, str(REPO / 'tests/e2e_server.py'), str(base), str(port)],
                              cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT)
    origin = f'http://localhost:{port}'
    out = {'pid': server.pid}
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{port}', headers={'origin': origin}, timeout=30) as client:
            for _ in range(240):
                if server.poll() is not None:
                    raise RuntimeError(f'e2e server exited with {server.returncode}; see {work / "e2e.log"}')
                try:
                    health = client.get('/api/health')
                    if health.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.5)
            else:
                raise RuntimeError('e2e server did not answer /api/health in 120 s')
            out['health'] = health.json()
            login = client.post('/api/auth/login', json={'username': rehearsal.OWNER, 'password': rehearsal.OWNER_PASSWORD,
                                                         'code': auth.totp(rehearsal.OWNER_TOTP, time.time())})
            out['login'] = login.status_code
            login.raise_for_status()
            client.headers['x-csrf-token'] = login.json().get('csrf') or client.cookies.get('acc_csrf') or ''

            def get(path):
                response = client.get(path)
                return response.status_code, (response.json() if response.headers.get('content-type', '').startswith(
                    'application/json') else None)

            reads = {}
            for name, path in [('projects', '/api/projects'), ('sessions', '/api/sessions?archived=1'), ('nodes', '/api/nodes'),
                               ('devices', '/api/devices'), ('vault', '/api/vault/keys'), ('dns_credentials', '/api/dns/credentials'),
                               ('dns_binding', '/api/dns/binding'), ('backups', '/api/host/backups'), ('users', '/api/users'),
                               ('groups', '/api/groups'), ('schedules', '/api/schedules'), ('attention', '/api/attention'),
                               ('telemetry', '/api/telemetry')]:
                status, body = get(path)
                reads[name] = {'status': status, 'size': len(body) if isinstance(body, list) else
                               (len(next((v for v in body.values() if isinstance(v, list)), [])) if isinstance(body, dict) else None)}
                reads[name]['body'] = body
            out['reads'] = {k: {'status': v['status'], 'size': v['size']} for k, v in reads.items()}
            out['seen'] = {'projects': sorted(str(p.get('slug')) for p in reads['projects']['body'] or []),
                           'vault': names(reads['vault']['body'])}
            out['checks'] = checks(reads, expected)
    finally:
        server.send_signal(signal.SIGTERM)  # only the process this script started
        try:
            server.wait(30)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()
        log.close()
    return out


def names(body, key='name'):
    items = body if isinstance(body, list) else next((v for v in (body or {}).values() if isinstance(v, list)), [])
    return sorted(str(i.get(key)) for i in items if isinstance(i, dict))


def checks(reads, expected) -> dict:
    """Spot checks: what the owner sees after the upgrade is what was seeded."""
    body = {k: v['body'] for k, v in reads.items()}
    targets = (body['backups'] or {}).get('targets', [])
    consent = (body['telemetry'] or {}).get('consent', {})
    return {
        'all reads 200': all(v['status'] == 200 for v in reads.values()),
        'projects': sorted(p['slug'] for p in body['projects'] if not p.get('builtin')) == sorted(expected['projects']),
        'users': names(body['users'], 'username') == sorted(expected['users']),
        'vault key names': names(body['vault']) == sorted(expected['vault']),
        'dns credentials': names(body['dns_credentials']) == sorted(expected['credentials']),
        'dns binding zone': (body['dns_binding'] or {}).get('zone', (body['dns_binding'] or {}).get('binding', {}).get('zone'))
        == 'example.com',
        'backup target kept': [t.get('node_name') for t in targets] == ['build-box'],
        'machines': {'build-box', 'studio-mac'} <= set(names(body['nodes'])),
        'group': names(body['groups']) == ['Platform team'],
        'telemetry consent': consent.get('usage') is True and consent.get('reports') is False,
        'browser devices': len(names(body['devices'], 'label')) >= 1,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--work', type=Path, required=True, help='a new or empty folder of your own')
    parser.add_argument('--port', type=int, required=True, help='a free port of your own for the e2e server')
    parser.add_argument('--postgres', type=Path, help='PostgreSQL bin folder (e.g. /usr/lib/postgresql/16/bin)')
    args = parser.parse_args()
    work = args.work.resolve()
    if work.exists() and any(work.iterdir()):
        parser.error(f'{work} is not empty')
    work.mkdir(parents=True, exist_ok=True)
    data = work / 'data'
    data.mkdir()
    result = {'backend': 'postgresql' if args.postgres else 'sqlite', 'base': rehearsal.BASE}
    cluster = Cluster(args.postgres, work) if args.postgres else None
    url = cluster.__enter__() if cluster else f'sqlite:///{work / "acc.db"}'
    try:
        rehearsal.upgrade(url, rehearsal.BASE)
        result['seeded'] = rehearsal.seed(url, data)
        if not args.postgres:
            shutil.copy2(work / 'acc.db', data / 'acc.db')  # the e2e server's own data folder gets this copy
            url = f'sqlite:///{data / "acc.db"}'
        base = rehearsal.schema(url)
        before = rehearsal.snapshot(url, base)
        seeded = rehearsal.rows(work / 'scratch-vault')  # names only; values are regenerated and never compared
        # What the owner's own lists show: group projects live under their group; private keys are their owner's.
        expected = {'projects': [p['slug'] for p in seeded['project'] if not p['group_id']],
                    'users': [u['username'] for u in seeded['user']],
                    'vault': [v['name'] for v in seeded['vault_key'] if v['owner'] == rehearsal.OWNER or v['shared']],
                    'credentials': [c['name'] for c in seeded['infrastructure_credential']]}
        result['e2e'] = probe(work, args.port, url, data, expected)
        # SQLite: the server upgraded its own copy (e2e/run-*/data/acc.db); compare that one.
        if not args.postgres:
            upgraded = next(Path((work / 'e2e-base').read_text()).glob('run-*/data/acc.db'))
            url = f'sqlite:///{upgraded}'
        result['head'] = rehearsal.version(url)
        after = rehearsal.snapshot(url, base)
        result['unchanged after upgrade'] = {t: before[t] == after[t] for t in before}
        result['changed by the app while running'] = sorted(t for t in before if before[t] != after[t])
        result['integrity after upgrade'] = rehearsal.integrity(url)
        result['newer-schema rows'] = rehearsal.after_upgrade(url)
        changed = set(result['changed by the app while running'])
        rehearsal.downgrade(url, rehearsal.BASE)
        result['after downgrade'] = {'version': rehearsal.version(url), 'schema is BASE': rehearsal.schema(url) == base,
                                     'integrity': rehearsal.integrity(url)}
        down = rehearsal.snapshot(url, base)
        result['after downgrade']['differences'] = {t: [len(before[t]), len(down[t])] for t in before
                                                    if before[t] != down[t] and t not in changed}
    finally:
        if cluster:
            cluster.__exit__(None, None, None)
        if (work / 'e2e-base').exists():  # the e2e server's state: synthetic, and this run's own folder
            shutil.rmtree((work / 'e2e-base').read_text(), ignore_errors=True)
        (work / 'result.json').write_text(json.dumps(result, indent=2, sort_keys=True, default=str))
    print(json.dumps({k: v for k, v in result.items() if k not in ('unchanged after upgrade',)}, indent=2, default=str))
    ok = (result['e2e']['login'] == 200 and all(result['e2e']['checks'].values()) and
          result['after downgrade']['version'] == rehearsal.BASE and result['after downgrade']['schema is BASE'])
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
