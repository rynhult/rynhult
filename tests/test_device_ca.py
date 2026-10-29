"""Client-only issuance and the root helper's closed request boundary."""
import base64
import importlib.util
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

HELPER = Path(__file__).resolve().parents[1] / 'deploy/device-ca/acc-device-ca.py'


def helper():
    spec = importlib.util.spec_from_file_location('device_ca', HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_client_identity_is_encrypted_bounded_and_has_no_server_authority(tmp_path):
    ca = helper()
    ca.initialize(tmp_path)
    result = ca.issue(tmp_path, {'operation': 'issue', 'device_id': 'a' * 32, 'days': 90})
    key, cert, chain = pkcs12.load_key_and_certificates(base64.b64decode(result['pkcs12']), result['password'].encode())
    assert key is not None and len(chain) == 1
    assert cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == 'a' * 32
    assert cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value == x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH])
    assert not cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    assert cert.not_valid_after_utc - datetime.now(timezone.utc) <= timedelta(days=90)
    assert cert.public_bytes(serialization.Encoding.PEM).decode() == result['certificate']
    with pytest.raises(ValueError):
        pkcs12.load_key_and_certificates(base64.b64decode(result['pkcs12']), b'wrong')
    assert sorted(p.name for p in tmp_path.iterdir()) == ['ca.key', 'ca.pem', 'issued.log']
    assert (tmp_path / 'ca.key').stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('body', [
    {}, {'operation': 'init'}, {'operation': 'issue', 'device_id': '../evil', 'days': 90},
    {'operation': 'issue', 'device_id': 'a' * 32, 'days': 91},
    {'operation': 'issue', 'device_id': 'a' * 32, 'days': True},
    {'operation': 'issue', 'device_id': 'a' * 32, 'days': 90, 'path': '/tmp/key'},
    {'operation': 'issue', 'device_id': 'a' * 32, 'days': 0},
])
def test_closed_contract_rejects_before_reading_keys(tmp_path, body):
    with pytest.raises(ValueError):
        helper().issue(tmp_path, body)
    assert list(tmp_path.iterdir()) == []


def test_cli_refuses_arguments_and_oversize_requests():
    import sys
    for args, body in [(['--path', '/tmp/evil'], '{}'), ([], 'x' * 4097), ([], json.dumps({'operation': 'init'}))]:
        result = subprocess.run([sys.executable, str(HELPER), *args], input=body, text=True, capture_output=True)
        assert result.returncode != 0
        assert 'PRIVATE KEY' not in result.stdout + result.stderr


def test_executable_success_uses_only_fixed_state_and_returns_encrypted_identity(tmp_path):
    import sys
    ca = helper()
    ca.initialize(tmp_path)
    # Execute the exact entry point against test-owned state; no production path
    # is opened, and runtime callers still have no state/path argument.
    code = """import importlib.util,sys
from pathlib import Path
spec=importlib.util.spec_from_file_location('signer',sys.argv[1])
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.STATE=Path(sys.argv[2]); sys.argv=['acc-device-ca']; sys.exit(m.main())
"""
    result = subprocess.run([sys.executable, '-c', code, str(HELPER), str(tmp_path)],
                            input=json.dumps({'operation': 'issue', 'device_id': 'b' * 32, 'days': 30}),
                            text=True, capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    key, cert, _ = pkcs12.load_key_and_certificates(base64.b64decode(response['pkcs12']), response['password'].encode())
    assert key is not None
    assert cert.not_valid_after_utc < datetime.now(timezone.utc) + timedelta(days=31)
    assert 'PRIVATE KEY' not in result.stdout + result.stderr
    assert sorted(p.name for p in tmp_path.iterdir()) == ['ca.key', 'ca.pem', 'issued.log']


def test_fixed_socket_client_round_trip_with_real_signer(tmp_path, monkeypatch):
    import socket
    import tempfile
    import threading
    from types import SimpleNamespace

    from acc.server import devices
    ca = helper()
    ca.initialize(tmp_path)
    errors = []
    with tempfile.TemporaryDirectory(prefix='acc-dca-', dir='/tmp') as scratch:
        endpoint = str(Path(scratch) / 'sign.sock')
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(endpoint)
            server.listen(1)
            server.settimeout(5)
            def serve():
                try:
                    connection, _ = server.accept()
                    with connection:
                        raw = bytearray()
                        while data := connection.recv(4096):
                            raw.extend(data)
                        connection.sendall(json.dumps(ca.issue(tmp_path, json.loads(raw))).encode())
                except Exception as error:
                    errors.append(error)
            thread = threading.Thread(target=serve)
            thread.start()
            class Client:
                def __init__(self):
                    self.sock = socket.socket(socket.AF_UNIX)
                def connect(self, path):
                    assert path == '/run/acc-device-ca.sock'
                    self.sock.connect(endpoint)
                def __getattr__(self, name):
                    return getattr(self.sock, name)
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    self.sock.close()
            monkeypatch.setattr(devices, 'socket', SimpleNamespace(socket=lambda *_: Client(), AF_UNIX=socket.AF_UNIX, SHUT_WR=socket.SHUT_WR))
            result = devices.sign({'operation': 'issue', 'device_id': 'c' * 32, 'days': 90})
            thread.join(timeout=5)
            assert not thread.is_alive() and not errors
            key, certificate, _ = pkcs12.load_key_and_certificates(base64.b64decode(result['pkcs12']), result['password'].encode())
            assert key is not None
            assert certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == 'c' * 32


@pytest.mark.parametrize('loose', ['dir', 'key'])
def test_signer_refuses_group_or_world_accessible_authority(tmp_path, loose):
    import os
    ca = helper()
    ca.initialize(tmp_path / 'state')
    os.chmod(tmp_path / 'state' if loose == 'dir' else tmp_path / 'state' / 'ca.key', 0o750 if loose == 'dir' else 0o640)
    with pytest.raises(ValueError):
        ca.issue(tmp_path / 'state', {'operation': 'issue', 'device_id': 'a' * 32, 'days': 1})


def test_signer_refuses_symlinked_key(tmp_path):
    import os
    ca = helper()
    ca.initialize(tmp_path / 'real')
    (tmp_path / 'state').mkdir(mode=0o700)
    os.symlink(tmp_path / 'real' / 'ca.key', tmp_path / 'state' / 'ca.key')
    (tmp_path / 'state' / 'ca.pem').write_bytes((tmp_path / 'real' / 'ca.pem').read_bytes())
    with pytest.raises(ValueError):
        ca.issue(tmp_path / 'state', {'operation': 'issue', 'device_id': 'a' * 32, 'days': 1})


def test_initialize_refuses_an_existing_loose_directory(tmp_path):
    import os
    (tmp_path / 'state').mkdir()
    os.chmod(tmp_path / 'state', 0o755)
    with pytest.raises(ValueError):
        helper().initialize(tmp_path / 'state')


def test_signer_keeps_an_issuance_ledger_and_bounds_its_rate(tmp_path):
    ca = helper()
    ca.initialize(tmp_path)
    serials = []
    for n in range(ca.ISSUE_LIMIT):
        result = ca.issue(tmp_path, {'operation': 'issue', 'device_id': f'{n:032x}', 'days': 1})
        serials.append(str(x509.load_pem_x509_certificate(result['certificate'].encode()).serial_number))
    ledger = [json.loads(line) for line in (tmp_path / 'issued.log').read_text().splitlines()]
    assert [entry['serial'] for entry in ledger] == serials
    assert set(ledger[0]) == {'issued_at', 'device_id', 'serial', 'not_after'}
    assert (tmp_path / 'issued.log').stat().st_mode & 0o777 == 0o600
    # A compromised caller cannot mint an unbounded stream of identities.
    with pytest.raises(ValueError):
        ca.issue(tmp_path, {'operation': 'issue', 'device_id': 'f' * 32, 'days': 1})
