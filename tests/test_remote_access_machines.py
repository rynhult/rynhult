"""The rendered machine/browser split and edge identity header, loaded into real, disposable Caddy."""
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from test_device_ca import helper as signer
from test_device_ca_machine import csr
from test_remote_access_readiness import disposable_edge, issue_client

from acc.node.config import write_atomic

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from remote_access_qualification.harness import CADDY, Lab  # noqa: E402

pytestmark = pytest.mark.skipif(not Path(CADDY).exists(), reason='needs /usr/bin/caddy')


def machine_client(ca, directory, lab, name, node_id):
    key, request = csr()
    issued = ca.issue(directory, {'operation': 'issue_machine', 'node_id': node_id, 'csr': request, 'days': 30})
    write_atomic(lab.root / f'{name}.pem', issued['certificate'].encode(), 0o600)
    write_atomic(lab.root / f'{name}.key', key.private_bytes(serialization.Encoding.PEM,
                 serialization.PrivateFormat.PKCS8, serialization.NoEncryption()), 0o600)
    return x509.load_pem_x509_certificate(issued['certificate'].encode())


def apply(edge, leaves):
    pems = [c.public_bytes(serialization.Encoding.PEM).decode() for c in leaves]
    review = edge.handle({'action': 'review', 'hostname': 'remote.example.com', 'leaves': pems})
    assert edge.handle({'action': 'apply', 'hostname': 'remote.example.com', 'leaves': pems,
                        'revision': review['revision']})['last_reload_result'] == 'verified'


def spoofed(lab, identity, value):
    ctx = lab.context(identity)
    with lab.connect(ctx) as sock:
        sock.sendall(f'GET / HTTP/1.1\r\nHost: remote.example.com\r\nX-Acc-Edge-Client: {value}\r\n'
                     f'X-Acc-Device-Id: forged\r\nConnection: close\r\n\r\n'.encode())
        return sock.recv(64)


def test_machine_and_browser_identities_reach_only_their_routes(tmp_path):
    ca = signer()
    authority_dir = tmp_path / 'authority'
    ca.initialize(authority_dir)
    with Lab() as lab:
        browser, authority = issue_client(ca, authority_dir, lab, 'browser', 'a' * 32)
        node = machine_client(ca, authority_dir, lab, 'machine', '0123456789abcdef')
        with disposable_edge(lab, tmp_path / 'edge', authority) as edge:
            apply(edge, [browser, node])
            machine_paths = ['/api/nodes/connect', '/api/nodes/bundle', '/api/git/7.git/info/refs']
            browser_only = ['/', '/api/projects']
            nobody = ['/api/nodes/enroll', '/api/nodes/install.sh', '/api/nodes/ca', '/install.sh', '/bundle',
                      '/pair-device', '/api/device-pairing/claim', '/preview/x']
            for path in machine_paths:
                assert lab.request('machine', path=path) == 200, path
                assert lab.request('browser', path=path) == 403, path
            for path in browser_only:
                assert lab.request('browser', path=path) == 200, path
                assert lab.request('machine', path=path) == 403, path
            for path in nobody:
                for identity in ('machine', 'browser'):
                    assert lab.request(identity, path=path) == 403, (identity, path)
            # The upstream learns the verified leaf from the edge alone; client-supplied assertions never pass.
            lab.seen.clear()
            assert lab.request('machine', path='/api/nodes/bundle') == 200
            assert lab.seen[-1][1]['X-Acc-Edge-Client'] == node.fingerprint(hashes.SHA256()).hex()
            assert spoofed(lab, 'browser', 'f' * 64).startswith(b'HTTP/1.1 200')
            path, headers = lab.seen[-1]
            assert headers['X-Acc-Edge-Client'] == browser.fingerprint(hashes.SHA256()).hex()
            assert 'X-Acc-Device-Id' not in headers
            # Private recovery stays without an identity header (accd treats it as private).
            assert lab.request(None, sni='acc.example.lan', host='acc.example.lan') == 200
            assert 'X-Acc-Edge-Client' not in lab.seen[-1][1]
            # Removing the machine: its leaf leaves the policy and new handshakes fail before HTTP.
            before = lab.count
            apply(edge, [browser])
            for tls in ('TLSv1_2', 'TLSv1_3'):
                assert lab.request('machine', path='/api/nodes/connect', tls=tls) is None
            assert lab.count == before
            assert lab.request('browser') == 200


def test_roaming_backup_context_presents_own_leaf_and_revocation_stops_it(tmp_path, monkeypatch):
    import ssl
    from datetime import datetime, timedelta, timezone

    from acc.node.git_backup import public_context

    ca = signer()
    authority_dir = tmp_path / 'authority'
    ca.initialize(authority_dir)
    node_id = '0123456789abcdef'
    state = tmp_path / 'node'
    state.mkdir(mode=0o700)
    with Lab() as lab:
        browser, authority = issue_client(ca, authority_dir, lab, 'browser', 'a' * 32)
        cert = machine_client(ca, authority_dir, lab, 'machine', node_id)
        write_atomic(state / 'machine.pem', (lab.root / 'machine.pem').read_bytes()
                     + (lab.root / 'machine.key').read_bytes(), 0o600)
        with disposable_edge(lab, tmp_path / 'edge', authority) as edge:
            apply(edge, [browser, cert])
            create = ssl.create_default_context
            with monkeypatch.context() as patch:
                # Substitute only the disposable server CA, never verification or client auth.
                patch.setattr(ssl, 'create_default_context', lambda: create(cafile=str(lab.root / 'server_ca.pem')))
                ctx = public_context(state, node_id)
                assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
                with pytest.raises(ValueError, match='machine identity'):
                    public_context(state, 'other-machine')
                class Later:
                    @staticmethod
                    def now(zone):
                        return datetime.now(timezone.utc) + timedelta(days=31)
                import acc.node.git_backup as transport
                patch.setattr(transport, 'datetime', Later)
                with pytest.raises(ValueError, match='machine identity'):
                    public_context(state, node_id)

            def request():
                try:
                    with lab.connect(ctx) as sock:
                        sock.sendall(b'GET /api/git/7.git/token HTTP/1.1\r\nHost: remote.example.com\r\nConnection: close\r\n\r\n')
                        return sock.recv(128)
                except OSError:
                    return None
            assert request().startswith(b'HTTP/1.1 200')
            assert lab.seen[-1][1]['X-Acc-Edge-Client'] == cert.fingerprint(hashes.SHA256()).hex()
            before = lab.count
            assert lab.request(None, path='/api/git/7.git/token') is None
            assert lab.count == before
            apply(edge, [browser])
            assert request() is None
            assert lab.count == before


def test_native_assertion_is_set_only_by_the_public_proxy_and_redacted_from_review(tmp_path):
    ca = signer()
    authority_dir = tmp_path / 'authority'
    ca.initialize(authority_dir)
    key, request = csr()
    native = ca.issue(authority_dir, {'operation': 'issue_native', 'device_id': 'd' * 32, 'csr': request, 'days': 90})
    with Lab() as lab:
        write_atomic(lab.root / 'native.pem', native['certificate'].encode(), 0o600)
        write_atomic(lab.root / 'native.key', key.private_bytes(serialization.Encoding.PEM,
                     serialization.PrivateFormat.PKCS8, serialization.NoEncryption()), 0o600)
        authority = x509.load_pem_x509_certificate((authority_dir / 'ca.pem').read_bytes())
        leaf = x509.load_pem_x509_certificate(native['certificate'].encode())
        with disposable_edge(lab, tmp_path / 'edge', authority) as edge:
            assertion = 'b1' * 32
            edge.policy['native_edge_key'] = assertion
            apply(edge, [leaf])
            with lab.connect(lab.context('native')) as sock:
                sock.sendall(b'GET /api/projects HTTP/1.1\r\nHost: remote.example.com\r\n'
                             b'X-Acc-Native-Edge: forged\r\nConnection: close\r\n\r\n')
                assert sock.recv(64).startswith(b'HTTP/1.1 200')
            assert lab.seen[-1][1]['X-Acc-Native-Edge'] == assertion
            assert lab.seen[-1][1]['X-Acc-Edge-Client'] == leaf.fingerprint(hashes.SHA256()).hex()
            assert lab.request(None, sni='acc.example.lan', host='acc.example.lan') == 200
            assert 'X-Acc-Native-Edge' not in lab.seen[-1][1]
            for path in ('/pair-mobile', '/api/mobile/v1/pairings', '/api/mobile/v1/claim',
                         '/api/mobile/v1/credential', '/api/mobile/v1/devices/abc/rotate'):
                assert lab.request('native', path=path) == 403


def streaming_upstream():
    """Answers /events with a slow event stream and anything else with a WebSocket-style upgrade that echoes."""
    server = socket.create_server(('127.0.0.1', 0))

    def one(conn):
        with conn:
            head = b''
            while b'\r\n\r\n' not in head:
                head += conn.recv(4096)
            if head.startswith(b'GET /events'):
                conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-cache\r\n\r\n')
                for tick in range(40):
                    try:
                        conn.sendall(b'data: tick%d\n\n' % tick)
                    except OSError:
                        return
                    time.sleep(.1)
                return
            conn.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n')
            while data := conn.recv(4096):
                conn.sendall(data)

    def accept():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            threading.Thread(target=one, args=(conn,), daemon=True).start()
    threading.Thread(target=accept, daemon=True).start()
    return server


def test_caddy_off_closes_upgraded_streams_but_not_streamed_responses(tmp_path):
    """Why accd closes public streams itself on Turn off (test_remote_access_off_streams)."""
    ca = signer()
    authority_dir = tmp_path / 'authority'
    ca.initialize(authority_dir)
    with Lab() as lab:
        if not lab.version.lstrip('v').startswith('2.6.'):
            pytest.skip(f'recorded for Caddy 2.6; re-qualify {lab.version}')
        browser, authority = issue_client(ca, authority_dir, lab, 'browser', 'a' * 32)
        upstream = streaming_upstream()
        try:
            with disposable_edge(lab, tmp_path / 'edge', authority, upstream.getsockname()[1]) as edge:
                apply(edge, [browser])
                events = lab.connect(lab.context('browser'))
                events.sendall(b'GET /events HTTP/1.1\r\nHost: remote.example.com\r\n\r\n')
                events.settimeout(3)
                first = b''
                while b'tick0' not in first:  # the headers can arrive before the first event
                    first += events.recv(4096)
                ws = lab.connect(lab.context('browser'))
                ws.sendall(b'GET /socket HTTP/1.1\r\nHost: remote.example.com\r\nUpgrade: websocket\r\n'
                           b'Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n'  # gitleaks:allow -- RFC 6455 example nonce, not a credential
                           b'Sec-WebSocket-Version: 13\r\n\r\n')
                assert ws.recv(4096).startswith(b'HTTP/1.1 101')
                ws.sendall(b'before')
                assert ws.recv(64) == b'before'
                assert edge.handle({'action': 'off'})['enabled'] is False
                assert lab.request('browser') is None  # new handshakes fail at once
                # The upgraded stream gets a WebSocket close (1001, going away) and then the connection ends.
                ws.settimeout(3)
                ws.sendall(b'after')
                assert ws.recv(64) == b'\x88\x02\x03\xe9'
                assert ws.recv(64) == b''
                # A response streaming since before OFF keeps flowing to the public client.
                events.settimeout(3)
                later = b''
                deadline = time.monotonic() + 1.5
                while time.monotonic() < deadline:
                    later += events.recv(4096)
                assert b'tick' in later
                events.close()
                ws.close()
        finally:
            upstream.close()
