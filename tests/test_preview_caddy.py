"""Adapt real Caddy templates: previews reuse the private gate and correct certificate issuer."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from acc.server.publish import PublishRefused, check_request

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('mode', ['wildcard', 'internal'])
def test_preview_caddy_routing_and_certificates(tmp_path, mode):
    caddy = shutil.which('caddy')
    if not caddy:
        pytest.skip('Caddy unavailable')
    template = (REPO / f'deploy/caddy/{mode}.caddy.in').read_text()
    text = template.replace('@ZONE@', 'example.com').replace('@ACC_HOST@', 'acc.example.com').replace('@PREVIEW_DOMAIN@', 'example.com' if mode == 'wildcard' else 'acc.example.lan')
    (tmp_path / 'routes-example.com').mkdir()
    config = tmp_path / 'Caddyfile'
    config.write_text('{\n admin unix//run/caddy/admin.sock|0660\n}\n' + text)
    result = subprocess.run([caddy, 'adapt', '--config', str(config), '--adapter', 'caddyfile'], capture_output=True, text=True, cwd=tmp_path, env={**os.environ, "CF_API_TOKEN": "test-only-canary-token-12345"})
    assert result.returncode == 0, result.stderr
    adapted = json.loads(result.stdout)
    # No standalone per-project certificates or public upstreams.
    servers = adapted['apps']['http']['servers']
    routes = json.dumps(servers)
    assert 'preview-' in routes
    assert 'unix//run/accd.sock' in routes
    for network in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '100.64.0.0/10', '127.0.0.0/8', '::1', 'fd00::/8'):
        assert network in routes
    assert '403' in routes
    tls = json.dumps(adapted['apps']['tls'])
    if mode == 'wildcard':
        assert 'cloudflare' in tls
        assert 'preview-' not in tls
    else:
        assert 'internal' in tls
        assert '*.acc.example.lan' in tls


def test_publishing_cannot_shadow_preview_namespace():
    with pytest.raises(PublishRefused):
        check_request('preview-1-8000.example.com', 8000, 'public', 'publish')
