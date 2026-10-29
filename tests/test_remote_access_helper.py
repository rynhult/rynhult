"""Privileged policy boundary: requests cannot select paths, commands or ungated routes."""
import copy
import json
import os

import pytest

from acc import remote_access_helper as rh


@pytest.fixture
def edge(tmp_path):
    from test_device_ca import helper
    ca = helper()
    authority = tmp_path / 'ca'
    ca.initialize(authority)
    issued = ca.issue(authority, {'operation': 'issue', 'device_id': 'a' * 32, 'days': 90})
    state = tmp_path / 'edge'
    state.mkdir(mode=0o700)
    base = {'admin': {'listen': 'unix//run/caddy/admin.sock'}, 'apps': {'http': {'servers': {'main': {
        'listen': [':443'], 'tls_connection_policies': [{'match': {'sni': ['acc.example.lan']}}],
        'routes': [{'match': [{'host': ['acc.example.lan']}], 'handle': [{'handler': 'static_response', 'status_code': 403}]}]
    }}}}}
    policy = {'qualified': True, 'hostnames': ['remote.example.com'], 'server': 'main'}
    effects = []
    loaded = [copy.deepcopy(base)]

    def runner(action, config):
        effects.append(action)
        if action == 'reload':
            loaded[0] = copy.deepcopy(config)
        if action == 'verify':
            return loaded[0] == config
        return True
    fd = os.open(state, os.O_RDONLY | os.O_DIRECTORY)
    subject = rh.RemoteAccessHelper(fd, policy, base, (authority / 'ca.pem').read_text(), runner, owner=os.getuid())
    yield subject, effects, issued, state, loaded
    os.close(fd)


def message(issued):
    return {'action': 'review', 'hostname': 'remote.example.com', 'leaves': [issued['certificate']]}


def test_review_apply_off_exact_revision_and_private_recovery(edge):
    subject, effects, issued, state, loaded = edge
    review = subject.handle(message(issued))
    assert effects == [] and not (state / 'current.json').exists()
    change = review['configuration']
    gate = change['tls_connection_policy']['client_authentication']
    assert gate['mode'] == 'require_and_verify'
    assert len(gate['verifiers'][0]['TrustedLeafCerts']) == 1
    assert change['server_settings']['strict_sni_host'] is True
    assert change['server_settings']['protocols'] == ['h1', 'h2']
    assert change['tls_session_tickets']['disabled'] is True
    assert change['base_revision'] == rh.revision(subject.base)
    config = subject.render('remote.example.com', [issued['certificate']])
    assert review['revision'] == rh.revision(config)
    applied = subject.handle({**message(issued), 'action': 'apply', 'revision': review['revision']})
    assert effects == ['verify', 'validate', 'reload', 'verify']
    assert applied['enabled'] and applied['devices_allowed'] == 1
    assert json.loads((state / 'current.json').read_text()) == config == loaded[0]
    off = subject.handle({'action': 'off'})
    assert off['enabled'] is False and off['devices_allowed'] == 0
    assert 'remote.example.com' not in json.dumps(loaded[0])
    assert 'acc.example.lan' in json.dumps(loaded[0])


@pytest.mark.parametrize('change', [
    {'hostname': 'acc.example.lan'}, {'hostname': 'evil.example.com'}, {'command': 'sh'},
    {'path': '/etc/caddy/Caddyfile'}, {'leaves': ['invalid']}, {'action': 'reload'},
])
def test_contract_refuses_before_effects(edge, change):
    subject, effects, issued, _, _ = edge
    with pytest.raises(ValueError):
        subject.handle({**message(issued), **change})
    assert effects == []


def test_stale_review_refused_and_zero_devices_retains_leaf_verifier(edge):
    subject, effects, issued, _, _ = edge
    review = subject.handle(message(issued))
    with pytest.raises(ValueError):
        subject.handle({'action': 'apply', 'hostname': 'remote.example.com', 'leaves': [], 'revision': review['revision']})
    assert not effects
    empty = subject.handle({'action': 'review', 'hostname': 'remote.example.com', 'leaves': []})
    gate = empty['configuration']['tls_connection_policy']['client_authentication']
    assert gate['verifiers'] == [{'verifier': 'leaf', 'TrustedLeafCerts': []}]


def test_unqualified_installation_cannot_enable_but_can_turn_off(edge):
    subject, effects, issued, _, _ = edge
    subject.policy['qualified'] = False
    review = subject.handle(message(issued))
    with pytest.raises(ValueError):
        subject.handle({**message(issued), 'action': 'apply', 'revision': review['revision']})
    assert not effects
    assert subject.handle({'action': 'off'})['enabled'] is False


def test_failed_reload_never_reports_applied_and_off_recovers(edge):
    subject, _, issued, state, _ = edge
    review = subject.handle(message(issued))
    original = subject.runner
    subject.runner = lambda action, config: False if action == 'reload' else original(action, config)
    with pytest.raises(ValueError):
        subject.handle({**message(issued), 'action': 'apply', 'revision': review['revision']})
    status = subject.handle({'action': 'status'})
    assert status['last_reload_result'] == 'failed' and status['enabled'] is None
    subject.runner = original
    assert subject.handle({'action': 'off'})['enabled'] is False


def test_symlink_destination_refused_before_reload(edge, tmp_path):
    subject, effects, issued, state, _ = edge
    victim = tmp_path / 'victim'
    victim.write_text('unchanged')
    (state / 'current.json').symlink_to(victim)
    review = subject.handle(message(issued))
    with pytest.raises(ValueError):
        subject.handle({**message(issued), 'action': 'apply', 'revision': review['revision']})
    assert not effects and victim.read_text() == 'unchanged'


def test_status_detects_out_of_band_caddy_restart(edge):
    subject, _, issued, _, loaded = edge
    review = subject.handle(message(issued))
    subject.handle({**message(issued), 'action': 'apply', 'revision': review['revision']})
    loaded[0] = copy.deepcopy(subject.base)
    status = subject.handle({'action': 'status'})
    assert status['enabled'] is None
    assert status['last_reload_result'] == 'configuration drift'


def test_validation_failure_keeps_last_verified_configuration(edge):
    subject, _, issued, state, loaded = edge
    review = subject.handle(message(issued))
    original = subject.runner
    subject.runner = lambda action, config: False if action == 'validate' else original(action, config)
    with pytest.raises(ValueError):
        subject.handle({**message(issued), 'action': 'apply', 'revision': review['revision']})
    assert not (state / 'current.json').exists()
    assert 'remote.example.com' not in json.dumps(loaded[0])


def test_never_overwrites_unrelated_caddy_changes_from_stale_base(edge):
    subject, _, issued, state, loaded = edge
    loaded[0]['apps']['new-published-site'] = {'keep': True}
    review = subject.handle(message(issued))
    with pytest.raises(ValueError):
        subject.handle({**message(issued), 'action': 'apply', 'revision': review['revision']})
    assert loaded[0]['apps']['new-published-site'] == {'keep': True}
    assert not (state / 'current.json').exists()


def test_production_commands_drop_privileges_and_use_fixed_binary_and_stdin(monkeypatch):
    from types import SimpleNamespace
    calls = []
    def command(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(rh.subprocess, 'run', command)
    configuration = {'apps': {}}
    assert rh.run_caddy('validate', configuration)
    args, options = calls[0]
    assert args == ['/usr/bin/caddy', 'validate', '--config', '-']
    assert options['input'] == b'{"apps":{}}'
    assert options['user'] == options['group'] == 'caddy'
    assert options['extra_groups'] == []
    assert options['timeout'] <= 5
    assert rh.run_caddy('reload', configuration)
    assert calls[1][0] == ['/usr/bin/caddy', 'reload', '--config', '-', '--address', 'unix//run/caddy/admin.sock']


def test_executable_refuses_arguments_with_sanitized_output():
    import subprocess
    import sys
    result = subprocess.run([sys.executable, '-m', 'acc.remote_access_helper', '--path', '/tmp/evil'],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 1
    assert json.loads(result.stdout) == {'error': 'remote_access_helper_refused'}
    assert result.stderr == ''


def test_entrypoint_off_does_not_read_device_ca(monkeypatch, tmp_path, capsys):
    import contextlib
    import io
    import socket
    from types import SimpleNamespace

    from acc import peercred

    class Files:
        def __init__(self, *args, **kwargs):
            pass
        def read(self, name):
            if name == 'policy.json':
                return '{"accd_uid":1234}'
            if name == 'base.json':
                return '{"admin":{"listen":"unix//run/caddy/admin.sock"}}'
            raise AssertionError('OFF must not depend on the device CA')
        def handle(self, message):
            assert message == {'action': 'off'}
            return {'enabled': False}
    monkeypatch.setattr(rh, 'RemoteAccessHelper', Files)
    monkeypatch.setattr(rh, 'open_root', lambda path: os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY))
    monkeypatch.setattr(rh, 'caddy_lock', lambda **kwargs: contextlib.nullcontext())
    monkeypatch.setattr(rh.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(rh.sys, 'argv', ['helper'])
    monkeypatch.setattr(rh.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(b'{"action":"off"}\n')))
    monkeypatch.setattr(peercred, 'peer_uid', lambda sock: 1234)
    duplicate = os.dup
    left, right = socket.socketpair()
    try:
        monkeypatch.setattr(rh.os, 'dup', lambda fd: duplicate(left.fileno()))
        assert rh.main() == 0
        assert json.loads(capsys.readouterr().out) == {'enabled': False}
    finally:
        left.close()
        right.close()


class Killed(BaseException):
    """A helper process death: no except clause in the helper runs."""


@pytest.mark.parametrize('failure', ['reload_fails', 'killed_before_reload'])
def test_failed_revocation_apply_can_still_turn_off(edge, failure):
    # The live configuration keeps the old allow list after a failed or interrupted apply.
    # OFF must still remove it, not refuse it as unrelated drift and leave a revoked leaf allowed.
    subject, _, issued, _, loaded = edge
    review = subject.handle(message(issued))
    subject.handle({**message(issued), 'action': 'apply', 'revision': review['revision']})
    enabled = copy.deepcopy(loaded[0])
    revoked = subject.handle({'action': 'review', 'hostname': 'remote.example.com', 'leaves': []})
    original = subject.runner
    def broken(action, config):
        if action == 'reload':
            if failure == 'killed_before_reload':
                raise Killed()
            return False
        return original(action, config)
    subject.runner = broken
    with pytest.raises((ValueError, Killed)):
        subject.handle({'action': 'apply', 'hostname': 'remote.example.com', 'leaves': [], 'revision': revoked['revision']})
    assert loaded[0] == enabled
    subject.runner = original
    assert subject.handle({'action': 'off'})['enabled'] is False
    assert loaded[0] == subject.base


def test_review_does_not_disclose_root_base(edge):
    # base.json holds DNS issuer credentials and every published site; accd and browsers see only the public change.
    subject, _, issued, _, loaded = edge
    subject.base['apps']['tls'] = {'automation': {'policies': [{'issuers': [{'module': 'acme', 'challenges': {
        'dns': {'provider': {'name': 'example', 'api_token': 'root-only-secret'}}}}]}]}}
    loaded[0] = copy.deepcopy(subject.base)
    review = subject.handle(message(issued))
    assert 'root-only-secret' not in json.dumps(review)
    assert 'acc.example.lan' not in json.dumps(review)
    assert review['configuration']['tls_connection_policy']['client_authentication']['mode'] == 'require_and_verify'
    assert review['configuration']['route']['match'] == [{'host': ['remote.example.com']}]
    assert subject.handle({**message(issued), 'action': 'apply', 'revision': review['revision']})['enabled'] is True


def test_enabling_keeps_automatic_https_for_unrelated_sites(edge):
    subject, _, issued, _, _ = edge
    config = subject.render('remote.example.com', [issued['certificate']])
    server = config['apps']['http']['servers']['main']
    assert server['automatic_https'] == {'skip': ['remote.example.com']}
    subject.base['apps']['http']['servers']['main']['automatic_https'] = {'skip': ['other.example.com']}
    server = subject.render('remote.example.com', [])['apps']['http']['servers']['main']
    assert server['automatic_https'] == {'skip': ['other.example.com', 'remote.example.com']}


def test_public_route_keeps_caddys_own_forwarded_address(edge):
    # Caddy applies header deletions after it sets X-Forwarded-For, so deleting it left accd with no client address.
    subject, _, issued, _, _ = edge
    route = subject.render('remote.example.com', [])['apps']['http']['servers']['main']['routes'][0]
    proxy = route['handle'][0]['routes'][-1]['handle'][0]
    deleted = proxy['headers']['request']['delete']
    assert not any(name.lower().startswith('x-forwarded') for name in deleted)
    assert {'X-Acc-Device-*', 'Forwarded', 'X-Real-Ip'} <= set(deleted)


def test_rendered_gate_in_disposable_caddy_forwarding_and_host_bypass(tmp_path):
    import http.client
    import sys
    from http.server import BaseHTTPRequestHandler
    from pathlib import Path

    from cryptography.hazmat.primitives import serialization
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
    from remote_access_qualification.harness import PRIVATE, PUBLIC, Lab
    seen = []
    with Lab() as lab:
        class Recorder(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append({k.lower(): v for k, v in self.headers.items()})
                self.send_response(200)
                self.send_header('Content-Length', '0')
                self.end_headers()

            def log_message(self, *args):
                pass
        lab.upstream.RequestHandlerClass = Recorder
        base = lab.config([])
        server = base['apps']['http']['servers']['qualification']
        server['tls_connection_policies'].pop(0)
        server['routes'].pop(0)
        folder = tmp_path / 'edge'
        folder.mkdir(mode=0o700)
        fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
        try:
            helper = rh.RemoteAccessHelper(fd, {'qualified': True, 'hostnames': [PUBLIC], 'server': 'qualification'}, base,
                                           (lab.root / 'device_ca.pem').read_text(), None, owner=os.getuid(),
                                           upstream=f'127.0.0.1:{lab.upstream.server_port}')
            leaf = lab.certs['active'].public_bytes(serialization.Encoding.PEM).decode()
            lab._admin('POST', '/load', helper.render(PUBLIC, [leaf]))
        finally:
            os.close(fd)
        with lab.connect(lab.context('active')) as sock:
            sock.sendall(f'GET / HTTP/1.1\r\nHost: {PUBLIC}\r\nX-Forwarded-For: 10.0.0.5\r\nForwarded: for=10.0.0.6\r\n'
                         f'X-Acc-Device-Id: forged\r\nX-Real-Ip: 10.0.0.7\r\nConnection: close\r\n\r\n'.encode())
            response = http.client.HTTPResponse(sock)
            response.begin()
            response.read()
        assert response.status == 200
        assert seen[-1]['x-forwarded-for'] == '127.0.0.1'
        assert not {'forwarded', 'x-acc-device-id', 'x-real-ip'} & set(seen[-1])
        count = len(seen)
        # No client certificate: a benign SNI, a missing SNI or Host variants never reach ACC.
        for sni, host in ((PRIVATE, PUBLIC), (PRIVATE, PUBLIC + ':443'), (PRIVATE, PUBLIC.upper()), (PRIVATE, PUBLIC + '.'),
                          (None, PUBLIC), (PUBLIC, PUBLIC)):
            assert lab.request(None, sni=sni, host=host) in (None, 421)
        assert lab.request('revoked') is None and lab.request('expired') is None and lab.request('wrong_ca') is None
        assert len(seen) == count


def test_native_policy_requires_separate_assertion_and_redacts_review(edge):
    from test_device_ca import helper
    from test_mobile import request_key
    subject, effects, _, state, loaded = edge
    _, csr = request_key()
    native = helper().issue(state.parent / 'ca', {'operation': 'issue_native', 'device_id': 'd' * 32, 'csr': csr, 'days': 90})
    request = message(native)
    with pytest.raises(ValueError, match='native_edge_not_provisioned'):
        subject.handle(request)
    assert effects == []
    key = 'a1' * 32
    subject.policy['native_edge_key'] = key
    review = subject.handle(request)
    assert key not in json.dumps(review)
    assert review['revision'] == rh.revision(subject.render('remote.example.com', [native['certificate']]))
    subject.handle({**request, 'action': 'apply', 'revision': review['revision']})
    assert key in json.dumps(loaded[0])  # installed only in the root-controlled public proxy
    assert key not in json.dumps(subject.handle({'action': 'status'}))
