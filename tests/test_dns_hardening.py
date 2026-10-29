"""Adapt deployment templates in temporary roots. No reload and no live config access."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('mode', ['internal', 'wildcard'])
def test_safe_logging_timeouts_and_internal_default(tmp_path, mode):
    caddy = shutil.which('caddy')
    if not caddy:
        pytest.skip('Caddy unavailable')
    text = (REPO / f'deploy/caddy/{mode}.caddy.in').read_text()
    text = text.replace('@ZONE@', 'example.com').replace('@ACC_HOST@', 'acc.example.lan').replace('@PREVIEW_DOMAIN@', 'example.lan')
    (tmp_path / 'routes-example.com').mkdir()
    shutil.copy(REPO / 'deploy/caddy/acc-hardening.options', tmp_path / 'acc-hardening.options')
    config = tmp_path / 'Caddyfile'
    config.write_text((REPO / 'deploy/caddy/Caddyfile').read_text().replace('import acc.d/*.caddy', '') + text)
    adapted = subprocess.run([caddy, 'adapt', '--config', str(config)], cwd=tmp_path, capture_output=True, text=True,
                             env={**os.environ, 'CF_API_TOKEN': 'CANARY-test-only-123456789'})
    assert adapted.returncode == 0, adapted.stderr
    result = json.loads(adapted.stdout)
    servers = result['apps']['http']['servers']
    for server in servers.values():
        assert server['read_header_timeout'] == 10_000_000_000
        assert server['idle_timeout'] == 120_000_000_000
        assert 'write_timeout' not in server
    logs = result['logging']['logs']
    access = [v for k, v in logs.items() if k != 'default']
    assert access
    for log in access:
        filters = log['encoder']['fields']
        assert filters['request>uri']['filter'] == 'delete'
        assert filters['request>headers']['filter'] == 'delete'
        assert filters['resp_headers']['filter'] == 'delete'
        assert log['writer']['roll_keep_days'] == 14
    if mode == 'internal':
        assert 'cloudflare' not in json.dumps(result)
        assert 'internal' in json.dumps(result['apps']['tls'])
        assert 'CANARY' not in adapted.stdout


def test_fresh_internal_ca_and_real_access_log_canaries(tmp_path):
    """Disposable Caddy process: no systemd, live roots, DNS API, trust-store installation or reload."""
    import socket
    import ssl
    import time
    import urllib.error
    import urllib.request
    caddy = shutil.which('caddy')
    if not caddy:
        pytest.skip('Caddy unavailable')
    with socket.socket() as reserved:
        reserved.bind(('127.0.0.1', 0))
        port = reserved.getsockname()[1]
    state = tmp_path / 'state'
    access = tmp_path / 'access.json'
    text = (REPO / 'deploy/caddy/internal.caddy.in').read_text().replace('@ACC_HOST@', f'localhost:{port}')
    text = text.replace('@PREVIEW_DOMAIN@', 'example.lan').replace('/var/lib/caddy/acc-access.json', str(access))
    text = text.replace('/run/accd.sock', str(tmp_path / 'absent-upstream.sock'))
    config = tmp_path / 'Caddyfile'
    config.write_text('{\n admin off\n skip_install_trust\n auto_https disable_redirects\n'
                      f' https_port {port}\n storage file_system {{\n root {state}\n }}\n}}\n' + text)
    process = subprocess.Popen([caddy, 'run', '--config', str(config)], cwd=tmp_path, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, env={**os.environ, 'HOME': str(tmp_path),
                                   'XDG_DATA_HOME': str(tmp_path / 'data'), 'XDG_CONFIG_HOME': str(tmp_path / 'config')})
    try:
        root = state / 'pki/authorities/local/root.crt'
        deadline = time.monotonic() + 10
        while not root.exists() and time.monotonic() < deadline and process.poll() is None:
            time.sleep(.05)
        assert root.exists(), 'temporary internal CA did not start'
        context = ssl.create_default_context(cafile=str(root))
        canary = 'dns-log-canary-0123456789'
        request = urllib.request.Request(f'https://localhost:{port}/?token={canary}',
            headers={'Authorization': 'Bearer ' + canary, 'Cookie': 'session=' + canary, 'X-Acc-Csrf': canary})
        while True:
            try:
                urllib.request.urlopen(request, context=context, timeout=1)
            except urllib.error.HTTPError as error:
                assert error.code == 502  # the deliberately absent temporary upstream
                break
            except (OSError, ssl.SSLError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.05)
        deadline = time.monotonic() + 5
        while (not access.exists() or not access.stat().st_size) and time.monotonic() < deadline:
            time.sleep(.05)
        lines = access.read_text()
        assert canary not in lines
        logged = json.loads(lines.splitlines()[-1])
        assert 'uri' not in logged['request'] and 'headers' not in logged['request']
        assert access.stat().st_mode & 0o077 == 0
    finally:
        process.terminate()
        process.wait(timeout=10)


def test_installer_validation_uses_caddy_home_after_privilege_drop(tmp_path, monkeypatch):
    import importlib.util
    import sys
    from types import SimpleNamespace
    spec = importlib.util.spec_from_file_location('caddy_check', REPO / 'deploy/lib/caddy-check.py')
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    monkeypatch.setattr(checker.os, 'geteuid', lambda: 0)
    monkeypatch.setenv('HOME', str(tmp_path / 'operator-home'))
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'operator-data'))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'operator-config'))
    monkeypatch.setattr(sys, 'argv', ['caddy-check', 'validate', '/usr/bin/caddy', 'Caddyfile', str(tmp_path / 'absent.env')])
    calls = []
    def run(argv, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(checker.subprocess, 'run', run)
    assert checker.main() == 0
    assert calls[0]['user'] == 'caddy' and calls[0]['extra_groups'] == []
    assert calls[0]['env']['HOME'] == '/var/lib/caddy'
    assert 'XDG_DATA_HOME' not in calls[0]['env'] and 'XDG_CONFIG_HOME' not in calls[0]['env']


def test_managed_template_adapts_with_explicit_candidate_environment(tmp_path):
    from acc.dns_credentials import environment_values
    caddy = shutil.which('caddy')
    if not caddy:
        pytest.skip('Caddy unavailable')
    text = (REPO / 'deploy/dns/wildcard-managed.caddy.in').read_text()
    text = text.replace('@ZONE@', 'example.com').replace('@ACC_HOST@', 'acc.example.lan')
    (tmp_path / 'routes-example.com').mkdir()
    shutil.copy(REPO / 'deploy/caddy/acc-hardening.options', tmp_path / 'acc-hardening.options')
    config = tmp_path / 'Caddyfile'
    config.write_text((REPO / 'deploy/caddy/Caddyfile').read_text().replace('import acc.d/*.caddy', '') + text)
    result = subprocess.run([caddy, 'adapt', '--config', str(config)], cwd=tmp_path, capture_output=True, text=True,
        env={'PATH': '/usr/bin:/bin', **environment_values('CF_API_TOKEN=candidate-canary-12345678\n')})
    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert parsed['admin']['config']['persist'] is False
    assert 'candidate-canary-12345678' in result.stdout
