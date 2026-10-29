"""The privileged native profile cannot sign arbitrary CSR authorities or names."""
from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import ExtendedKeyUsageOID
from test_device_ca import helper
from test_mobile import request_key


def test_native_profile_keeps_private_key_on_device_and_discards_requested_authority(tmp_path):
    ca = helper()
    ca.initialize(tmp_path)
    key, csr = request_key()
    result = ca.issue(tmp_path, {'operation': 'issue_native', 'device_id': 'd' * 32, 'csr': csr, 'days': 90})
    assert set(result) == {'certificate'}
    cert = x509.load_pem_x509_certificate(result['certificate'].encode())
    assert cert.public_key().public_numbers() == key.public_key().public_numbers()
    assert cert.subject.rfc4514_string() == 'OU=acc-native,CN=' + 'd' * 32
    assert not cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    assert set(cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value) == {ExtendedKeyUsageOID.CLIENT_AUTH}
    with pytest.raises(x509.ExtensionNotFound):
        cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    assert cert.not_valid_after_utc - datetime.now(timezone.utc) <= timedelta(days=90)
    cert.verify_directly_issued_by(x509.load_pem_x509_certificate((tmp_path / 'ca.pem').read_bytes()))


@pytest.mark.parametrize('change', [{'days': 91}, {'days': True}, {'days': 0}, {'device_id': '../x'},
                                  {'csr': 'x' * 2049}, {'csr': 'not a CSR'}, {'path': '/tmp/key'}])
def test_native_signer_closed_contract(tmp_path, change):
    ca = helper()
    _, csr = request_key()
    with pytest.raises(ValueError):
        ca.issue(tmp_path, {'operation': 'issue_native', 'device_id': 'd' * 32, 'csr': csr, 'days': 90, **change})
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('wrong_curve', [True, False])
def test_native_signer_requires_p256_and_a_valid_signature(tmp_path, wrong_curve):
    ca = helper()
    ca.initialize(tmp_path)
    if wrong_curve:
        key = ed25519.Ed25519PrivateKey.generate()
        csr = x509.CertificateSigningRequestBuilder().subject_name(x509.Name([])).sign(key, None)
    else:
        _, pem = request_key()
        der = bytearray(x509.load_pem_x509_csr(pem.encode()).public_bytes(serialization.Encoding.DER))
        der[-5] ^= 0xff
        csr = x509.load_der_x509_csr(bytes(der))
    with pytest.raises(ValueError):
        ca.issue(tmp_path, {'operation': 'issue_native', 'device_id': 'd' * 32,
                           'csr': csr.public_bytes(serialization.Encoding.PEM).decode(), 'days': 90})
    assert not (tmp_path / 'issued.log').exists()


def test_native_and_browser_issuance_share_the_persistent_quota(tmp_path):
    ca = helper()
    ca.initialize(tmp_path)
    ca.ISSUE_LIMIT = 2
    _, csr = request_key()
    native = {'operation': 'issue_native', 'device_id': 'd' * 32, 'csr': csr, 'days': 90}
    ca.issue(tmp_path, native)
    ca.issue(tmp_path, {'operation': 'issue', 'device_id': 'a' * 32, 'days': 30})
    with pytest.raises(ValueError, match='rate'):
        ca.issue(tmp_path, native)
    assert len((tmp_path / 'issued.log').read_text().splitlines()) == 2


def test_native_certificate_survives_the_existing_encrypted_ca_restore_drill(tmp_path):
    ca = helper()
    original, restored = tmp_path / 'original', tmp_path / 'restored'
    ca.initialize(original)
    _, csr = request_key()
    request = {'operation': 'issue_native', 'device_id': 'd' * 32, 'csr': csr, 'days': 90}
    before = ca.issue(original, request)
    authority = x509.load_pem_x509_certificate((original / 'ca.pem').read_bytes())
    blob = ca.export_backup(original, b'example backup passphrase for native test')
    ca.restore_backup(blob, b'example backup passphrase for native test', restored, ca.fingerprint(authority))
    x509.load_pem_x509_certificate(before['certificate'].encode()).verify_directly_issued_by(
        x509.load_pem_x509_certificate((restored / 'ca.pem').read_bytes()))
    after = ca.issue(restored, request)
    x509.load_pem_x509_certificate(after['certificate'].encode()).verify_directly_issued_by(authority)
    assert after['certificate'] != before['certificate']


def test_native_leaf_is_admitted_by_caddy_only_when_in_the_enabled_registry(tmp_path):
    import base64
    import sys
    from pathlib import Path

    from cryptography.hazmat.primitives.serialization import pkcs12
    from sqlalchemy.orm import sessionmaker

    from acc.server import devices
    from acc.server.db import make_engine, migrate
    from acc.server.models import Device, Setting, User
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
    from remote_access_qualification.harness import Lab
    ca = helper()
    ca.initialize(tmp_path / 'authority')
    key, csr = request_key()
    native = ca.issue(tmp_path / 'authority', {'operation': 'issue_native', 'device_id': 'd' * 32, 'csr': csr, 'days': 90})
    browser = ca.issue(tmp_path / 'authority', {'operation': 'issue', 'device_id': 'b' * 32, 'days': 90})
    browser_key, browser_cert, _ = pkcs12.load_key_and_certificates(base64.b64decode(browser['pkcs12']), browser['password'].encode())
    cert = x509.load_pem_x509_certificate(native['certificate'].encode())
    engine = make_engine('sqlite:///' + str(tmp_path / 'registry.db'))
    migrate(str(engine.url))
    try:
        with Lab() as lab, sessionmaker(engine)() as s:
            (lab.root / 'device_ca.pem').write_bytes((tmp_path / 'authority/ca.pem').read_bytes())
            for label, leaf, private_key in [('native', cert, key), ('browser', browser_cert, browser_key)]:
                (lab.root / (label + '.pem')).write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
                (lab.root / (label + '.key')).write_bytes(private_key.private_bytes(serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            s.add(User(username='alex', password_hash='unused', totp_secret='unused'))
            s.add(Setting(key='remote_access', value={'public_hostname': 'remote.example.com'}))
            for id, leaf, kind in [('d' * 32, cert, 'native'), ('b' * 32, browser_cert, 'browser')]:
                s.add(Device(id=id, person='alex', label='Phone', kind=kind, status='active',
                    certificate_pem=leaf.public_bytes(serialization.Encoding.PEM).decode(),
                    fingerprint=devices.fingerprint(leaf), serial=str(leaf.serial_number), expires_at=leaf.not_valid_after_utc))
            s.commit()
            def apply(enabled):
                lab.reload_fragment(devices.policy_fragment(s, (lab.root / 'device_ca.pem').read_text(), mobile_enabled=enabled))
            apply(True)
            assert lab.request('native') == 200
            assert lab.request('browser') == 200
            apply(False)
            assert lab.request('native') is None  # denied during TLS, before the fixture upstream
            assert lab.request('browser') == 200
            assert lab.count == 3
    finally:
        engine.dispose()
