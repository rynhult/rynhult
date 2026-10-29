"""Operational drills against real, disposable Caddy and test-owned signer state.

These characterize the shipped preparation boundary. They do not qualify
established connections, production backup encryption, or public machine access.
"""
import base64
import json
import os
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import pkcs12
from helpers import TOTP_AT, write_db
from test_auth import PASSWORD
from test_device_ca import helper as signer
from test_devices import enable
from test_passkeys import setup

from acc.node.config import write_atomic
from acc.remote_access_helper import RemoteAccessHelper
from acc.server import auth, devices, remote_access
from acc.server.models import Device, User

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from remote_access_qualification.harness import Lab  # noqa: E402


@contextmanager
def disposable_edge(lab, directory, authority, upstream_port=None):
    """Use the product helper's complete render/validate/reload/verify boundary."""
    base = lab.config([])
    server = base['apps']['http']['servers']['qualification']
    server['tls_connection_policies'].pop(0)
    server['routes'].pop(0)
    lab._admin('POST', '/load', base)
    directory.mkdir(mode=0o700)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)

    def runner(action, config):
        if action == 'validate':
            return subprocess.run(['/usr/bin/caddy', 'validate', '--config', '-'],
                input=json.dumps(config).encode(), env=lab.env, capture_output=True, timeout=10).returncode == 0
        if action == 'reload':
            lab._admin('POST', '/load', config)
            return True
        return json.loads(lab._admin('GET', '/config/')) == config

    try:
        yield RemoteAccessHelper(fd,
            {'qualified': True, 'hostnames': ['remote.example.com'], 'server': 'qualification'},
            base, [a.public_bytes(serialization.Encoding.PEM).decode()
                   for a in (authority if isinstance(authority, list) else [authority])], runner,
            owner=os.getuid(), upstream=f'127.0.0.1:{upstream_port or lab.upstream.server_port}')
    finally:
        os.close(fd)


def issue_client(ca, directory, lab, name, device_id):
    issued = ca.issue(directory, {'operation': 'issue', 'device_id': device_id, 'days': 90})
    key, cert, chain = pkcs12.load_key_and_certificates(
        base64.b64decode(issued['pkcs12']), issued['password'].encode())
    # Test-owned leaf files only. No production authority or OS identity store.
    write_atomic(lab.root / f'{name}.pem', cert.public_bytes(serialization.Encoding.PEM), 0o600)
    write_atomic(lab.root / f'{name}.key', key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()), 0o600)
    return cert, chain[0]


def register(api, device_id, cert):
    with write_db(api.app.state.db) as s:
        person = s.query(User).filter_by(role='owner').first().username
        s.add(Device(id=device_id, person=person, label='Example device', kind='browser', status='active',
            certificate_pem=cert.public_bytes(serialization.Encoding.PEM).decode(),
            fingerprint=devices.fingerprint(cert), serial=str(cert.serial_number), expires_at=cert.not_valid_after_utc))
        s.commit()


async def apply(api, headers):
    review = await api.get('/api/remote-access/review')
    assert review.status_code == 200, review.text
    result = await api.post('/api/remote-access/apply', headers=headers, json={'revision': review.json()['revision']})
    assert result.status_code == 200, result.text
    assert result.json()['enabled'] is True and result.json()['pending'] is False


@pytest.mark.parametrize('removal', ['revoke', 'remove'])
async def test_lost_device_refused_within_one_minute_without_disabling_other_device(
        api, env, monkeypatch, tmp_path, removal):
    headers, _ = await setup(api, env, monkeypatch)
    await enable(api, headers)
    ca = signer()
    authority_dir = tmp_path / 'authority'
    ca.initialize(authority_dir)
    with Lab() as lab:
        lost, authority = issue_client(ca, authority_dir, lab, 'lost', 'a' * 32)
        survivor, _ = issue_client(ca, authority_dir, lab, 'survivor', 'b' * 32)
        register(api, 'a' * 32, lost)
        register(api, 'b' * 32, survivor)
        with disposable_edge(lab, tmp_path / 'edge', authority) as edge:
            async def request(body):
                return edge.handle(body)
            monkeypatch.setattr(remote_access, 'request', request)
            await apply(api, headers)
            for name in ('lost', 'survivor'):
                assert lab.request(name) == 200
            before, started = lab.count, time.monotonic()
            if removal == 'revoke':
                result = await api.post('/api/devices/' + 'a' * 32 + '/revoke', headers=headers, json={})
            else:
                result = await api.delete('/api/devices/' + 'a' * 32, headers=headers)
            assert result.status_code == 200, result.text
            for tls in ('TLSv1_2', 'TLSv1_3'):
                assert lab.request('lost', tls=tls) is None
            assert time.monotonic() - started < 60
            assert lab.count == before
            assert edge.handle({'action': 'status'})['devices_allowed'] == 1
            assert lab.request('survivor') == 200
            assert lab.request(None, sni='acc.example.lan', host='acc.example.lan') == 200
            assert (await api.get('/api/devices')).status_code == 401


async def test_ca_restore_preserves_old_identity_but_never_restores_revoked_membership(
        api, env, monkeypatch, tmp_path):
    """A restored CA must not imply restoring a stale allow list or enable intent.

    copytree models an owner-decrypted backup in an isolated fixture only; this
    is not an encryption or production backup/restore implementation.
    """
    headers, secret = await setup(api, env, monkeypatch)
    await enable(api, headers)
    ca = signer()
    original, backup, restored = (tmp_path / name for name in ('original', 'backup', 'restored'))
    ca.initialize(original)
    with Lab() as lab:
        survivor, authority = issue_client(ca, original, lab, 'survivor', 'a' * 32)
        lost, _ = issue_client(ca, original, lab, 'lost', 'b' * 32)
        register(api, 'a' * 32, survivor)
        register(api, 'b' * 32, lost)
        # The backup predates revocation. Recovery keeps the CURRENT database.
        shutil.copytree(original, backup)
        with disposable_edge(lab, tmp_path / 'edge', authority) as edge:
            async def request(body):
                return edge.handle(body)
            monkeypatch.setattr(remote_access, 'request', request)
            await apply(api, headers)
            assert lab.request('survivor') == lab.request('lost') == 200
            result = await api.post('/api/devices/' + 'b' * 32 + '/revoke', headers=headers, json={})
            assert result.status_code == 200, result.text
            assert (await api.get('/api/devices')).status_code == 401
            # Device revocation ends all owner sessions. Recovery requires a new
            # password/TOTP sign-in and fresh verification before another apply.
            monkeypatch.setattr(auth.time, 'time', lambda: TOTP_AT + 60)
            result = await api.post('/api/auth/login', headers={'Origin': headers['Origin']},
                json={'username': 'casey', 'password': PASSWORD, 'code': auth.totp(secret, TOTP_AT + 60)})
            assert result.status_code == 200, result.text
            headers['X-ACC-CSRF'] = api.cookies.get('acc_csrf')
            monkeypatch.setattr(auth.time, 'time', lambda: TOTP_AT + 90)
            result = await api.post('/api/auth/passkeys/reauth', headers=headers,
                json={'password': PASSWORD, 'code': auth.totp(secret, TOTP_AT + 90)})
            assert result.status_code == 200, result.text
            assert (await api.post('/api/remote-access/off', headers=headers, json={})).status_code == 200
            shutil.rmtree(original)
            shutil.copytree(backup, restored)
            # A fresh helper import represents a replacement signer instance.
            recovered_ca = signer()
            fresh, restored_authority = issue_client(recovered_ca, restored, lab, 'fresh', 'c' * 32)
            assert restored_authority == authority
            survivor.verify_directly_issued_by(restored_authority)
            fresh.verify_directly_issued_by(authority)
            register(api, 'c' * 32, fresh)
            # Restoring signer state alone neither admits a leaf nor enables routing.
            await api.app.state.ctx.remote_access.reconcile()
            assert (await api.get('/api/remote-access')).json()['enabled'] is False
            assert lab.request('survivor') is None
            await apply(api, headers)
            before = lab.count
            for tls in ('TLSv1_2', 'TLSv1_3'):
                assert lab.request('lost', tls=tls) is None
                assert lab.request(None, tls=tls) is None
            assert lab.count == before
            for tls in ('TLSv1_2', 'TLSv1_3'):
                assert lab.request('survivor', tls=tls) == 200
                assert lab.request('fresh', tls=tls) == 200
            assert edge.handle({'action': 'status'})['devices_allowed'] == 2
            assert lab.request(None, sni='acc.example.lan', host='acc.example.lan') == 200
