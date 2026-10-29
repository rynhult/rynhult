"""Machine identity profile and the root-only encrypted authority backup/restore drill."""
import base64
import importlib.util
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from test_device_ca import helper

RECOVERY = Path(__file__).resolve().parents[1] / 'deploy/device-ca/acc-device-ca-recovery.py'
PASSPHRASE = b'correct horse battery staple example\n'


def recovery():
    spec = importlib.util.spec_from_file_location('device_ca_recovery', RECOVERY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def csr(key=None, name='evil.example.com', ca=False):
    key = key or ec.generate_private_key(ec.SECP256R1())
    builder = (x509.CertificateSigningRequestBuilder()
               .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
               .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
               .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False))
    return key, builder.sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM).decode()


def machine(ca, directory, node_id='c' * 32, days=30, key=None):
    key, request = csr(key)
    issued = ca.issue(directory, {'operation': 'issue_machine', 'node_id': node_id, 'csr': request, 'days': days})
    return key, x509.load_pem_x509_certificate(issued['certificate'].encode()), issued


def test_machine_identity_signs_only_the_node_key_with_a_distinct_profile(tmp_path):
    ca = helper()
    ca.initialize(tmp_path)
    key, cert, issued = machine(ca, tmp_path)
    assert set(issued) == {'certificate'}  # the node keeps its key; no bundle or password exists
    assert cert.public_key().public_numbers() == key.public_key().public_numbers()
    assert cert.subject.rfc4514_string() == 'OU=acc-machine,CN=' + 'c' * 32
    assert not cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    assert set(cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value) == {ExtendedKeyUsageOID.CLIENT_AUTH}
    names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert names.get_values_for_type(x509.UniformResourceIdentifier) == ['urn:acc:machine:' + 'c' * 32]
    assert names.get_values_for_type(x509.DNSName) == []  # requested names and extensions are ignored
    assert cert.not_valid_after_utc - datetime.now(timezone.utc) <= timedelta(days=30)
    authority = x509.load_pem_x509_certificate((tmp_path / 'ca.pem').read_bytes())
    cert.verify_directly_issued_by(authority)
    assert '"machine_id": "' + 'c' * 32 in (tmp_path / 'issued.log').read_text()
    # A browser identity never carries the machine marker the edge routes on.
    browser = ca.issue(tmp_path, {'operation': 'issue', 'device_id': 'a' * 32, 'days': 90})
    _, leaf, _ = pkcs12.load_key_and_certificates(base64.b64decode(browser['pkcs12']), browser['password'].encode())
    assert 'acc-machine' not in leaf.subject.rfc4514_string()


@pytest.mark.parametrize('change', [
    {'days': 31}, {'days': 0}, {'days': True}, {'node_id': '../x'}, {'node_id': 'C' * 32},
    {'csr': 'x' * 2049}, {'csr': 'not a csr'}, {'path': '/tmp/key'},
])
def test_machine_contract_is_closed(tmp_path, change):
    ca = helper()
    ca.initialize(tmp_path)
    _, request = csr()
    body = {'operation': 'issue_machine', 'node_id': 'c' * 32, 'csr': request, 'days': 30, **change}
    with pytest.raises(ValueError):
        ca.issue(tmp_path, body)
    assert not (tmp_path / 'issued.log').exists()


def test_machine_csr_needs_proof_of_possession_and_p256(tmp_path):
    ca = helper()
    ca.initialize(tmp_path)
    other = ed25519.Ed25519PrivateKey.generate()
    request = (x509.CertificateSigningRequestBuilder().subject_name(x509.Name([]))
               .sign(other, None).public_bytes(serialization.Encoding.PEM).decode())
    with pytest.raises(ValueError):
        ca.issue(tmp_path, {'operation': 'issue_machine', 'node_id': 'c' * 32, 'csr': request, 'days': 30})
    _, good = csr()
    der = bytearray(x509.load_pem_x509_csr(good.encode()).public_bytes(serialization.Encoding.DER))
    der[-5] ^= 0xFF  # corrupt the signature
    tampered = x509.load_der_x509_csr(bytes(der)).public_bytes(serialization.Encoding.PEM).decode()
    with pytest.raises(ValueError):
        ca.issue(tmp_path, {'operation': 'issue_machine', 'node_id': 'c' * 32, 'csr': tampered, 'days': 30})


def test_encrypted_backup_restore_drill(tmp_path):
    ca = helper()
    authority = tmp_path / 'authority'
    ca.initialize(authority)
    _, before, _ = machine(ca, authority)
    recorded = ca.fingerprint(x509.load_pem_x509_certificate((authority / 'ca.pem').read_bytes()))
    blob = ca.export_backup(authority, PASSPHRASE.strip())
    for secret in ((authority / 'ca.key').read_bytes(), b'PRIVATE KEY', b'BEGIN CERTIFICATE'):
        assert secret not in blob
    ledger = (authority / 'issued.log').read_text()
    # Lose the authority completely, then restore into new private state.
    for name in ('ca.key', 'ca.pem', 'issued.log'):
        (authority / name).unlink()
    authority.rmdir()
    restored = tmp_path / 'restored'
    with pytest.raises(ValueError, match='passphrase'):
        ca.restore_backup(blob, b'a wrong passphrase value', restored, recorded)
    with pytest.raises(ValueError, match='fingerprint'):
        ca.restore_backup(blob, PASSPHRASE.strip(), restored, 'f' * 64)
    with pytest.raises(ValueError, match='fingerprint'):
        ca.restore_backup(blob, PASSPHRASE.strip(), restored, '')
    altered = bytearray(blob)
    altered[-1] ^= 1
    with pytest.raises(ValueError, match='altered'):
        ca.restore_backup(bytes(altered), PASSPHRASE.strip(), restored, recorded)
    assert not restored.exists()
    assert ca.restore_backup(blob, PASSPHRASE.strip(), restored, recorded) == recorded
    assert restored.stat().st_mode & 0o777 == 0o700
    assert all((restored / n).stat().st_mode & 0o777 == 0o600 for n in ('ca.key', 'ca.pem', 'issued.log'))
    assert (restored / 'issued.log').read_text() == ledger  # the issuance ledger and its rate window survive
    _, after, _ = machine(ca, restored, node_id='d' * 32)
    authority_cert = x509.load_pem_x509_certificate((restored / 'ca.pem').read_bytes())
    before.verify_directly_issued_by(authority_cert)
    after.verify_directly_issued_by(authority_cert)
    with pytest.raises(FileExistsError):  # never merges into or replaces an existing authority
        ca.restore_backup(blob, PASSPHRASE.strip(), restored, recorded)


def test_backup_refuses_a_loose_authority_and_a_short_passphrase(tmp_path):
    ca = helper()
    ca.initialize(tmp_path / 'a')
    with pytest.raises(ValueError):
        ca.export_backup(tmp_path / 'a', b'short')
    os.chmod(tmp_path / 'a' / 'ca.key', 0o644)
    with pytest.raises(ValueError):
        ca.export_backup(tmp_path / 'a', PASSPHRASE.strip())


def test_recovery_tool_uses_fixed_secret_and_prints_only_the_fingerprint(tmp_path):
    ca, tool = helper(), recovery()
    authority, secrets = tmp_path / 'authority', tmp_path / 'secret'
    ca.initialize(authority)
    secrets.mkdir(mode=0o700)
    secret = secrets / 'backup-passphrase'
    secret.write_bytes(PASSPHRASE)
    secret.chmod(0o600)
    out = tmp_path / 'device-ca.enc'
    printed = tool.run(['backup', str(out)], state=authority, secret=secret)
    assert printed == ca.fingerprint(x509.load_pem_x509_certificate((authority / 'ca.pem').read_bytes()))
    assert out.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        tool.run(['backup', str(out)], state=authority, secret=secret)
    for argv in (['backup', 'relative.enc'], ['restore', str(out)], ['issue'], []):
        with pytest.raises(ValueError):
            tool.run(argv, state=authority, secret=secret)
    assert tool.run(['restore', str(out), printed], state=tmp_path / 'new', secret=secret) == printed
    secret.chmod(0o640)
    with pytest.raises(ValueError):
        tool.run(['backup', str(tmp_path / 'other.enc')], state=authority, secret=secret)
    # The executable refuses non-root callers and never echoes secrets.
    result = subprocess.run([sys.executable, str(RECOVERY), 'backup', str(tmp_path / 'x.enc')],
                            capture_output=True, text=True, timeout=10)
    # Unprivileged runs are refused for not being root; a root-run suite reaches the next fixed refusal (the test files aren't private to the signer).
    expected = 'run as root' if os.geteuid() else 'must be private to the signer'
    assert result.returncode == 1 and expected in result.stderr
    assert 'PRIVATE' not in result.stdout + result.stderr


def test_server_backup_seals_the_authority_or_skips_it(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[1] / 'deploy/backup/acc-backup'
    spec = importlib.util.spec_from_loader('acc_backup', loader=None)
    module = importlib.util.module_from_spec(spec)
    module.__file__ = str(script)
    exec(compile(script.read_text(), str(script), 'exec'), module.__dict__)
    monkeypatch.setattr(module, 'DEST', tmp_path)
    monkeypatch.setattr(module, 'DEVICE_CA', tmp_path / 'missing')
    assert module.backup_device_ca('2026-10-24') == 'device CA: not provisioned'
    monkeypatch.setattr(module, 'DEVICE_CA', tmp_path)
    monkeypatch.setattr(module, 'DEVICE_CA_RECOVERY', tmp_path / 'absent')
    assert 'NOT backed up' in module.backup_device_ca('2026-10-24')
    assert not (tmp_path / '2026-10-24').exists()  # never a plain copy
    calls = []
    tool = tmp_path / 'tool'
    tool.write_text('')
    secret = tmp_path / 'secret'
    secret.write_text('x')
    monkeypatch.setattr(module, 'DEVICE_CA_RECOVERY', tool)
    monkeypatch.setattr(module, 'DEVICE_CA_PASSPHRASE', secret)
    monkeypatch.setattr(module.subprocess, 'run', lambda args, **kw: calls.append(args) or
                        subprocess.CompletedProcess(args, 0, 'ab' * 32 + '\n', ''))
    assert 'encrypted' in module.backup_device_ca('2026-10-24')
    assert calls == [[str(tool), 'backup', str(tmp_path / '2026-10-24' / 'device-ca.enc')]]
