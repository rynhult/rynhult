"""Device authority client, transient delivery, and Caddy 2.6.2 policy rendering.

Nothing in this module writes Caddy or applies a policy. The root helper owns the
CA. accd retains only leaf certificates; bundles live in memory for five minutes.
"""
import base64
import json
import socket
import threading
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization

from acc.remote_access_helper import revision

from . import auth
from .models import Setting

DEFAULTS = {'pairing_enabled': False, 'machine_identities': False, 'public_hostname': '', 'private_origin': '', 'certificate_days': 90}
PAIRING_SECONDS = 300
IMPORT_SECONDS = 300


def settings(s):
    row = s.get(Setting, 'remote_access')
    from .remote_access import state
    runtime = state(s)
    config = {**DEFAULTS, **(row.value if row else {})}
    return {**config, 'settings_revision': revision(config), **runtime,
            'applied': runtime['last_reload_result'] == 'verified', 'remote_previews': False}


def validate_names(public, private):
    import re
    if public and (len(public) > 253 or not re.fullmatch(r'[a-z0-9]+(?:[.-][a-z0-9]+)*\.[a-z]{2,63}', public)):
        raise ValueError('Enter the public hostname alone, such as acc.example.com, without https://, a port or a path.')
    parsed = urlsplit(private)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.path or parsed.query or parsed.fragment or parsed.port not in (None, 443)):
        raise ValueError('Enter the private address as https:// and a hostname only, for example https://acc.example.lan.')
    if parsed.hostname == public:
        raise ValueError('The public hostname must differ from the private recovery address.')


def sign(body):
    """Fixed socket, bounded input/output, no shell or caller-supplied paths."""
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(15)
        sock.connect('/run/acc-device-ca.sock')
        sock.sendall(json.dumps(body).encode())
        sock.shutdown(socket.SHUT_WR)
        chunks = bytearray()
        while data := sock.recv(8192):
            chunks.extend(data)
            if len(chunks) > 65536:
                raise ValueError('invalid signer response')
    return json.loads(chunks)


class Imports:
    """Bounded transient secrets, with active expiry even when nobody polls."""
    def __init__(self):
        self.lock = threading.Lock()
        self.values = {}

    def put(self, key, value):
        with self.lock:
            if len(self.values) >= 100:
                raise ValueError('Too many devices are waiting to finish pairing. Wait five minutes and try again.')
            self.values[key] = value
        timer = threading.Timer(IMPORT_SECONDS, self.pop, (key,))
        timer.daemon = True
        timer.start()

    def get(self, key):
        with self.lock:
            return self.values.get(key)

    def pop(self, key):
        with self.lock:
            return self.values.pop(key, None)


def identity(result, device_id, days):
    """Validate privileged output before registry/policy use."""
    from datetime import timedelta

    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    cert = x509.load_pem_x509_certificate(result['certificate'].encode())
    if (cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value != device_id
            or cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
            or set(cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value) != {ExtendedKeyUsageOID.CLIENT_AUTH}
            or cert.not_valid_after_utc > auth.now() + timedelta(days=days, minutes=1)
            or cert.not_valid_after_utc <= auth.now()):
        raise ValueError('invalid device identity')
    return cert


def client_authentication(s, ca_pem, mobile_enabled=False):
    def der(pem):
        return base64.b64encode(x509.load_pem_x509_certificate(pem.encode()).public_bytes(serialization.Encoding.DER)).decode()
    # Only a current person's device or an active node's machine: a row left behind by any removal path stays out.
    from .edge import active_leaves
    leaves = active_leaves(s, mobile_enabled)
    return {'mode': 'require_and_verify', 'trusted_ca_certs': [der(ca_pem)],
            'verifiers': [{'verifier': 'leaf', 'TrustedLeafCerts': [{'Raw': der(d.certificate_pem)} for d in leaves]}]}


def policy_fragment(s, ca_pem, mobile_enabled=False):
    config = settings(s)
    if not config['public_hostname']:
        raise ValueError('Set a separate public hostname before rendering.')
    # A fragment, deliberately no listener, routes, upstream, admin address or apply capability.
    return {'apps': {'tls': {'session_tickets': {'disabled': True}}, 'http': {'servers': {'remote': {
        'automatic_https': {'disable': True}, 'protocols': ['h1', 'h2'], 'strict_sni_host': True,
        'tls_connection_policies': [{'match': {'sni': [config['public_hostname']]},
                                    'client_authentication': client_authentication(s, ca_pem, mobile_enabled)}]}}}}}


def metadata(row):
    result = {k: getattr(row, k) for k in ('id', 'person', 'label', 'kind', 'status', 'fingerprint', 'serial',
                                       'expires_at', 'last_seen_at', 'created_at')}
    if row.status == 'active' and row.expires_at <= auth.now():
        result['status'] = 'expired'
    return result


def fingerprint(cert):
    return cert.fingerprint(hashes.SHA256()).hex()


def mobileconfig(bundle):
    """Apple client identity profile. The owner supplies its password separately."""
    import plistlib
    import uuid
    identifier = 'com.acc.device.' + uuid.uuid4().hex
    return plistlib.dumps({
        'PayloadType': 'Configuration', 'PayloadVersion': 1,
        'PayloadIdentifier': identifier, 'PayloadUUID': str(uuid.uuid4()).upper(),
        'PayloadDisplayName': 'Rynhult browser identity', 'PayloadRemovalDisallowed': False,
        'PayloadDescription': 'Client identity for Rynhult. Use the separate import password from your trusted device.',
        'PayloadContent': [{
            'PayloadType': 'com.apple.security.pkcs12', 'PayloadVersion': 1,
            'PayloadIdentifier': identifier + '.identity', 'PayloadUUID': str(uuid.uuid4()).upper(),
            'PayloadDisplayName': 'Rynhult device identity', 'PayloadContent': bundle,
        }],
    })


def matching_code(row):
    # Bound to the winning receiver, not the transferable QR token. No schema or secret storage needed.
    import hashlib
    if not row.receipt_hash:
        return None
    raw = hashlib.sha256((row.token_hash + row.receipt_hash).encode()).digest()
    return f'{int.from_bytes(raw[:4]) % 1000000:06d}'
