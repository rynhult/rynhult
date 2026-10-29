#!/usr/bin/env python3
"""Performance check: a large synthetic installation (tests/perf_seed.py) in the e2e server, then the main API
endpoints timed as the synthetic owner. Page loads: web/scripts/perf-pages.mjs against the same server (--keep).

    PYTHONPATH=src:tests .venv/bin/python scripts/perf_check.py --work /tmp/<short folder> --port <your e2e port>

The work folder must be new or empty and short (the e2e broker's Unix socket must fit in 107 bytes). The server
started here is stopped by its PID at the end unless --keep is given. Invented data only; never a live instance.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import statistics
import subprocess
import sys
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO / 'src'), str(REPO / 'tests')]

import perf_seed  # noqa: E402

from acc.server import auth  # noqa: E402

LONG_SESSION = f's{perf_seed.SCALE["sessions"] - 1:031d}'
ENDPOINTS = ['/api/sessions', '/api/attention', '/api/projects', '/api/nodes', '/api/host', '/api/audit',
             f'/api/sessions/{LONG_SESSION}', f'/api/sessions/{LONG_SESSION}/events?limit=300', '/api/updates',
             '/api/health']


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--runs', type=int, default=5)
    parser.add_argument('--keep', action='store_true', help='leave the server running for perf-pages.mjs')
    parser.add_argument('--running', type=int, default=perf_seed.SCALE['running'],
                        help='seeded sessions marked running (0 for an idle installation: they can never start)')
    args = parser.parse_args()
    work = args.work.resolve()
    if work.exists() and any(work.iterdir()):
        parser.error(f'{work} is not empty')
    seed = work / 'seed'
    started = time.perf_counter()
    counts = perf_seed.seed(f'sqlite:///{seed / "acc.db"}', seed, {'running': args.running})
    print(f'seeded {sum(counts.values()):,} rows in {time.perf_counter() - started:.0f} s: {counts}', flush=True)
    log = (work / 'server.log').open('w')
    server = subprocess.Popen([sys.executable, str(REPO / 'tests/e2e_server.py'), str(work / 'e2e'), str(args.port)],
                              cwd=REPO, stdout=log, stderr=subprocess.STDOUT,
                              env={**os.environ, 'PYTHONPATH': f"{REPO / 'src'}:{REPO / 'tests'}",
                                   'ACC_E2E_SEED_DATA': str(seed)})
    (work / 'server.pid').write_text(str(server.pid))
    origin = f'http://localhost:{args.port}'
    results = {}
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{args.port}', headers={'origin': origin}, timeout=120) as client:
            for _ in range(240):
                if server.poll() is not None:
                    raise SystemExit(f'e2e server exited ({server.returncode}); see {work / "server.log"}')
                try:
                    if client.get('/api/health').status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.5)
            client.post('/api/auth/login', json={'username': perf_seed.OWNER, 'password': perf_seed.OWNER_PASSWORD,
                                                 'code': auth.totp(perf_seed.OWNER_TOTP, time.time())}).raise_for_status()
            for path in ENDPOINTS:
                times, response = [], None
                for _ in range(args.runs):
                    begin = time.perf_counter()
                    response = client.get(path)
                    times.append((time.perf_counter() - begin) * 1000)
                results[path] = {'status': response.status_code, 'bytes': len(response.content),
                                 'median_ms': round(statistics.median(times)), 'max_ms': round(max(times))}
                print(f"{results[path]['median_ms']:7} ms  max {results[path]['max_ms']:6}  "
                      f"{results[path]['bytes']:>9} B  {response.status_code}  {path}", flush=True)
            etag = client.get('/api/sessions').headers.get('etag')
            again = client.get('/api/sessions', headers={'If-None-Match': etag or ''})
            results['/api/sessions unchanged'] = {'status': again.status_code, 'bytes': len(again.content)}
            print(f'unchanged /api/sessions: {again.status_code}, {len(again.content)} bytes')
    finally:
        if not args.keep:
            server.send_signal(signal.SIGTERM)  # only the server this script started
            try:
                server.wait(60)
            except subprocess.TimeoutExpired:
                server.kill()
        log.close()
    (work / 'results.json').write_text(json.dumps({'counts': counts, 'endpoints': results}, indent=2))
    if args.keep:
        print(f'server left running, PID {server.pid}; pages: node web/scripts/perf-pages.mjs {origin} '
              f'{perf_seed.OWNER_TOTP} {perf_seed.OWNER_PASSWORD} {LONG_SESSION} [--phone]')
    return 0


if __name__ == '__main__':
    sys.exit(main())
