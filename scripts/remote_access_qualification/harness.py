"""Exercise /usr/bin/caddy, isolated configuration, keys, storage and admin socket.

Only tests start listeners. All TCP listeners bind 127.0.0.1 on kernel-assigned
high ports; no DNS, system trust, live configuration or ACC upstream is used.
"""
from __future__ import annotations

import base64
import http.client
import json
import os
import socket
import ssl
import struct
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

CADDY = '/usr/bin/caddy'
PUBLIC = 'remote.example.com'
PRIVATE = 'acc.example.lan'


class Lab:
    """Context manager owning every process, socket, key and scratch file."""

    def __enter__(self):
        self.scratch = tempfile.TemporaryDirectory(prefix='acc-ra-', dir='/tmp')
        self.root = Path(self.scratch.name)
        self.process = self.upstream = self.thread = self.log = None
        self.count = 0
        self.seen = []  # (path, headers) the upstream received
        self.lock = threading.Lock()
        try:
            self.version = subprocess.check_output([CADDY, 'version'], text=True).strip()
            self._certificates()
            lab = self

            class Stub(BaseHTTPRequestHandler):
                def do_GET(self):
                    with lab.lock:
                        lab.count += 1
                        lab.seen.append((self.path, dict(self.headers)))
                    self.send_response(200)
                    self.send_header('Content-Length', '4')
                    self.end_headers()
                    self.wfile.write(b'stub')

                def log_message(self, *args):
                    pass

            self.upstream = ThreadingHTTPServer(('127.0.0.1', 0), Stub)
            self.thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
            self.thread.start()
            # Release only immediately before Caddy binds. A collision fails startup,
            # never falls back to another listener or a live admin endpoint.
            with socket.socket() as reservation:
                reservation.bind(('127.0.0.1', 0))
                self.port = reservation.getsockname()[1]
            self.admin = self.root / 'admin.sock'
            self.env = {**os.environ, 'HOME': str(self.root),
                        'XDG_DATA_HOME': str(self.root / 'data'),
                        'XDG_CONFIG_HOME': str(self.root / 'config')}
            self.path = self.root / 'caddy.json'
            self.path.write_text(json.dumps(self.config(['active', 'expired', 'wrong_ca'])))
            self.log = (self.root / 'caddy.log').open('w+')
            self.process = subprocess.Popen([CADDY, 'run', '--config', str(self.path)],
                                            cwd=self.root, env=self.env, stdout=self.log, stderr=self.log)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    self.log.seek(0)
                    raise RuntimeError(self.log.read())
                if self.admin.exists():
                    # /config/ is available only once this disposable admin started.
                    try:
                        self._admin('GET', '/config/')
                        break
                    except OSError:
                        pass
                time.sleep(.02)
            else:
                raise TimeoutError('Disposable Caddy startup timed out')
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *args):
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.upstream is not None:
            self.upstream.shutdown()
            self.upstream.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)
        if self.log is not None:
            self.log.close()
        self.scratch.cleanup()

    def _certificates(self):
        self.certs = {}
        now = datetime.now(timezone.utc)

        def issue(name, issuer=None, *, ca=False, server=False, expired=False):
            key = ec.generate_private_key(ec.SECP256R1())
            subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
            issuer_cert, issuer_key = issuer or (None, key)
            cert = (x509.CertificateBuilder().subject_name(subject)
                    .issuer_name(issuer_cert.subject if issuer_cert else subject)
                    .public_key(key.public_key()).serial_number(x509.random_serial_number())
                    .not_valid_before(now - timedelta(days=2))
                    .not_valid_after(now + timedelta(days=-1 if expired else 2))
                    .add_extension(x509.BasicConstraints(ca=ca, path_length=0 if ca else None), critical=True))
            if not ca:
                cert = cert.add_extension(x509.ExtendedKeyUsage([
                    ExtendedKeyUsageOID.SERVER_AUTH if server else ExtendedKeyUsageOID.CLIENT_AUTH]), critical=True)
            if server:
                cert = cert.add_extension(x509.SubjectAlternativeName([
                    x509.DNSName(PUBLIC), x509.DNSName(PRIVATE)]), critical=False)
            cert = cert.sign(issuer_key, hashes.SHA256())
            (self.root / f'{name}.pem').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            key_path = self.root / f'{name}.key'
            key_path.touch(mode=0o600)
            key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                                   serialization.PrivateFormat.PKCS8,
                                                   serialization.NoEncryption()))
            self.certs[name] = cert
            return cert, key

        device_ca = issue('device_ca', ca=True)
        wrong_ca = issue('other_ca', ca=True)
        server_ca = issue('server_ca', ca=True)
        issue('server', server_ca, server=True)
        issue('active', device_ca)
        issue('revoked', device_ca)  # valid chain, deliberately absent from active leaves
        issue('expired', device_ca, expired=True)
        issue('wrong_ca', wrong_ca)

    def config(self, leaves, *, tickets=False):
        def der(name):
            return base64.b64encode(self.certs[name].public_bytes(serialization.Encoding.DER)).decode()

        # 2.6.2's leaf module exposes x509.Certificate objects, not PEM strings.
        # Raw is what Certificate.Equal compares. An explicit empty module denies
        # all; omitting the module or using trusted_leaf_certs: [] does NOT.
        auth = {'mode': 'require_and_verify', 'trusted_ca_certs': [der('device_ca')],
                'verifiers': [{'verifier': 'leaf', 'TrustedLeafCerts': [{'Raw': der(n)} for n in leaves]}]}
        proxy = {'handler': 'reverse_proxy',
                 'upstreams': [{'dial': f'127.0.0.1:{self.upstream.server_port}'}]}
        return {
            'admin': {'listen': f'unix/{self.admin}', 'origins': ['localhost']},
            'storage': {'module': 'file_system', 'root': str(self.root / 'storage')},
            'apps': {
                'tls': {'certificates': {'load_files': [{'certificate': str(self.root / 'server.pem'),
                                                       'key': str(self.root / 'server.key')}]},
                        'session_tickets': {'disabled': not tickets}},
                'http': {'servers': {'qualification': {
                    'listen': [f'127.0.0.1:{self.port}'],
                    'automatic_https': {'disable': True}, 'protocols': ['h1', 'h2'],
                    'strict_sni_host': True,
                    'tls_connection_policies': [
                        {'match': {'sni': [PUBLIC]}, 'client_authentication': auth},
                        {'match': {'sni': [PRIVATE]}}],
                    'routes': [
                        {'match': [{'host': [PUBLIC]}], 'handle': [proxy]},
                        {'match': [{'host': [PRIVATE], 'remote_ip': {'ranges': [
                            '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '100.64.0.0/10',
                            '127.0.0.0/8', '::1', 'fd00::/8']}}], 'handle': [proxy]},
                        {'handle': [{'handler': 'static_response', 'status_code': 403}]}],
                }}}}}

    def _admin(self, method, path, body=None):
        with socket.socket(socket.AF_UNIX) as sock:
            sock.settimeout(5)
            sock.connect(str(self.admin))
            payload = json.dumps(body).encode() if body is not None else b''
            sock.sendall(f'{method} {path} HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n'
                         f'Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n'.encode() + payload)
            response = http.client.HTTPResponse(sock)
            response.begin()
            data = response.read()
            if response.status != 200:
                raise RuntimeError(f'Disposable admin {response.status}: {data!r}')
            return data

    def reload(self, leaves, *, tickets=False):
        start = time.monotonic()
        self._admin('POST', '/load', self.config(leaves, tickets=tickets))
        self.reload_seconds = time.monotonic() - start
        return self.reload_seconds

    def reload_fragment(self, fragment):
        """Exercise the product's rendered gate on this disposable listener only.

        Keep the lab's certificate, routes, admin socket and LAN recovery policy;
        take every public TLS gate field directly from the production renderer.
        """
        config = self.config([])
        config['apps']['tls']['session_tickets'] = fragment['apps']['tls']['session_tickets']
        rendered = fragment['apps']['http']['servers']['remote']
        server = config['apps']['http']['servers']['qualification']
        for field in ('automatic_https', 'protocols', 'strict_sni_host'):
            server[field] = rendered[field]
        server['tls_connection_policies'][0] = rendered['tls_connection_policies'][0]
        start = time.monotonic()
        self._admin('POST', '/load', config)
        self.reload_seconds = time.monotonic() - start
        return self.reload_seconds

    def context(self, identity, tls='TLSv1_3', protocol='http/1.1'):
        ctx = ssl.create_default_context(cafile=str(self.root / 'server_ca.pem'))
        # Unknown/empty SNI are adversarial probes, not server-trust failures.
        ctx.check_hostname = False
        ctx.minimum_version = ctx.maximum_version = getattr(ssl.TLSVersion, tls)
        ctx.set_alpn_protocols([protocol])
        if identity:
            ctx.load_cert_chain(self.root / f'{identity}.pem', self.root / f'{identity}.key')
        return ctx

    def connect(self, ctx, sni=PUBLIC, session=None):
        raw = socket.create_connection(('127.0.0.1', self.port), timeout=3)
        try:
            return ctx.wrap_socket(raw, server_hostname=sni or None, session=session)
        except BaseException:
            raw.close()
            raise

    def request(self, identity, *, sni=PUBLIC, host=PUBLIC, tls='TLSv1_3', path='/'):
        try:
            with self.connect(self.context(identity, tls), sni) as sock:
                sock.sendall(f'GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n'.encode())
                response = http.client.HTTPResponse(sock)
                response.begin()
                response.read()
                return response.status
        except (ssl.SSLError, ConnectionResetError):
            return None

    def revocation_probe(self, tls):
        ctx = self.context('active', tls)
        with self.connect(ctx) as sock:
            sock.sendall(f'GET / HTTP/1.1\r\nHost: {PUBLIC}\r\n\r\n'.encode())
            response = http.client.HTTPResponse(sock)
            response.begin()
            assert response.status == 200 and response.read() == b'stub'
            session = sock.session
            assert not session.has_ticket, 'Session resumption must be disabled'
        before = self.count
        assert self.reload(['expired', 'wrong_ca']) < 2, 'Local policy apply exceeded 2 seconds'
        # Explicitly offer the previous SSLSession; the full handshake must fail.
        try:
            with self.connect(ctx, session=session) as sock:
                assert not sock.session_reused
                sock.sendall(f'GET / HTTP/1.1\r\nHost: {PUBLIC}\r\n\r\n'.encode())
                assert not sock.recv(4096), 'Revoked session reached HTTP'
        except (ssl.SSLError, ConnectionResetError):
            pass
        for _ in range(5):
            assert self.request('active', tls=tls) is None
        assert self.count == before

    def old_ticket_probe(self, tls):
        # Deliberately unsafe baseline only inside this disposable lab. Establish
        # that a real ticket resumes before testing rejection after policy apply.
        self.reload(['active'], tickets=True)
        ctx = self.context('active', tls)

        def exchange(sock):
            sock.sendall(f'GET / HTTP/1.1\r\nHost: {PUBLIC}\r\n\r\n'.encode())
            response = http.client.HTTPResponse(sock)
            response.begin()
            assert response.status == 200 and response.read() == b'stub'

        with self.connect(ctx) as sock:
            exchange(sock)
            session = sock.session
            assert session.has_ticket, 'Positive control did not obtain a real session ticket'
        with self.connect(ctx, session=session) as sock:
            exchange(sock)
            assert sock.session_reused, 'Positive control did not actually resume TLS'
            session = sock.session
        before = self.count
        assert self.reload([]) < 2
        try:
            with self.connect(ctx, session=session) as sock:
                assert not sock.session_reused, 'Old ticket resumed after revocation'
                sock.sendall(f'GET / HTTP/1.1\r\nHost: {PUBLIC}\r\n\r\n'.encode())
                assert not sock.recv(4096), 'Old ticket reached HTTP after revocation'
        except (ssl.SSLError, ConnectionResetError):
            pass
        assert self.count == before

    def coalescing_probe(self, sni, identity, target):
        # Minimal RFC 7540 client: literal, uncompressed HPACK request headers.
        # Both names share one SAN cert and IP, so cross-origin reuse is possible.
        def frame(kind, flags, stream, payload):
            return len(payload).to_bytes(3, 'big') + bytes([kind, flags]) + struct.pack('!I', stream) + payload

        def literal(name, value):
            a, b = name.encode(), value.encode()
            assert len(a) < 127 and len(b) < 127
            return b'\x00' + bytes([len(a)]) + a + bytes([len(b)]) + b

        def exact(sock, length):
            data = b''
            while len(data) < length:
                chunk = sock.recv(length - len(data))
                if not chunk:
                    raise EOFError('HTTP/2 connection closed unexpectedly')
                data += chunk
            return data

        with self.connect(self.context(identity, protocol='h2'), sni) as sock:
            assert sock.selected_alpn_protocol() == 'h2'
            sock.sendall(b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n' + frame(4, 0, 0, b''))

            def request(stream, authority):
                headers = b'\x82\x87\x84' + literal(':authority', authority)
                sock.sendall(frame(1, 5, stream, headers))  # END_HEADERS | END_STREAM
                body, saw_headers = b'', False
                while True:
                    header = exact(sock, 9)
                    length, kind, flags = int.from_bytes(header[:3], 'big'), header[3], header[4]
                    received_stream = int.from_bytes(header[5:], 'big') & 0x7fffffff
                    payload = exact(sock, length)
                    if kind == 4 and not flags & 1:
                        sock.sendall(frame(4, 1, 0, b''))
                    assert kind not in (3, 7), f'HTTP/2 reset/goaway: {payload!r}'
                    if received_stream != stream:
                        continue
                    if kind == 1:
                        saw_headers = True
                    if kind == 0:
                        body += payload
                    if kind in (0, 1) and flags & 1:
                        assert saw_headers
                        return body

            before = self.count
            assert request(1, sni) == b'stub'
            assert self.count == before + 1
            assert request(3, target) != b'stub'
            assert self.count == before + 1, 'Coalesced request crossed the device gate'
            assert request(5, sni) == b'stub'
            assert self.count == before + 2
