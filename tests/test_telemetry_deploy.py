"""Validate deployment files through their actual configuration consumers."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_caddy_preserves_full_wire_size_cap():
    if not shutil.which('caddy'):
        pytest.skip('Caddy is not installed; target VM must validate configuration')
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(['caddy', 'adapt', '--config', str(root / 'deploy/telemetry/Caddyfile'),
                             '--adapter', 'caddyfile'], check=True, capture_output=True, text=True)
    config = json.loads(result.stdout)
    server = config['apps']['http']['servers']['srv0']
    route = next(h for h in server['routes'][0]['handle'][0]['routes'][0]['handle'] if h['handler'] == 'request_body')
    # Decimal KB would reject valid 16-KiB signed bodies before they reach ACC.
    assert route['handler'] == 'request_body'
    assert route['max_size'] == 16384
