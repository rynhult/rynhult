"""Pairing capabilities must not bypass person, owner, LAN or one-use checks."""
from datetime import timedelta

import pytest
from helpers import pause_supervisor, write_db
from race_helpers import guard_database, transaction_rendezvous
from sqlalchemy import select
from test_passkeys import setup

from acc.server import auth
from acc.server.models import AuditLog


async def enable(api, headers):
    result = await api.put('/api/remote-access', headers=headers, json={
        'pairing_enabled': True, 'public_hostname': 'remote.example.com', 'private_origin': 'https://acc.example.lan',
        'certificate_days': 90})
    assert result.status_code == 200, result.text


@pytest.fixture
def signer(monkeypatch, tmp_path):
    from test_device_ca import helper

    from acc.server import devices
    ca = helper()
    ca.initialize(tmp_path)
    monkeypatch.setattr(devices, 'sign', lambda body: ca.issue(tmp_path, body))


async def confirmation_body(api, pending):
    # The owner types the code read from the receiving device; tests read the same derivation.
    from acc.server import devices
    from acc.server.models import DevicePairing
    with api.app.state.db() as s:
        return {'matching_code': devices.matching_code(s.get(DevicePairing, pending['id']))}


async def start(api, headers):
    result = await api.post('/api/devices/pairings', headers=headers, json={'label': 'Alex phone'})
    assert result.status_code == 200, result.text
    return result.json()


async def test_off_by_default_and_cannot_enable_public_access(api, env, monkeypatch):
    headers, _ = await setup(api, env, monkeypatch)
    state = await api.get('/api/remote-access')
    assert state.status_code == 200, state.text
    assert state.json()['enabled'] is False
    assert state.json()['pairing_enabled'] is False
    assert (await api.post('/api/devices/pairings', headers=headers, json={'label': 'Phone'})).status_code == 409
    assert (await api.put('/api/remote-access', headers=headers, json={'enabled': True})).status_code == 422
    api.headers['Authorization'] = 'Bearer ' + api.app.state.ctx.token
    assert (await api.get('/api/devices')).status_code == 403


async def test_remote_settings_reject_stale_revision_without_changing_pairing(api, env, monkeypatch):
    headers, _ = await setup(api, env, monkeypatch)
    await enable(api, headers)
    original = (await api.get('/api/remote-access')).json()
    assert 'settings_revision' in original
    draft = {key: original[key] for key in ('pairing_enabled', 'public_hostname', 'private_origin', 'certificate_days')}
    first = await api.put('/api/remote-access', headers=headers, json={
        **draft, 'pairing_enabled': False, 'expected_revision': original['settings_revision']})
    assert first.status_code == 200, first.text
    stale = await api.put('/api/remote-access', headers=headers, json={
        **draft, 'certificate_days': 30, 'expected_revision': original['settings_revision']})
    assert stale.status_code == 409, stale.text
    state = (await api.get('/api/remote-access')).json()
    assert state['pairing_enabled'] is False and state['certificate_days'] == 90
    # The response advances the edit's revision; an owner can save again without reloading.
    again = await api.put('/api/remote-access', headers=headers, json={
        **draft, 'pairing_enabled': False, 'certificate_days': 30,
        'expected_revision': first.json()['settings_revision']})
    assert again.status_code == 200, again.text


@pytest.mark.parametrize('enabled', [True, False])
async def test_stale_tab_cannot_change_machine_identities(api, env, monkeypatch, enabled):
    headers, _ = await setup(api, env, monkeypatch)
    await enable(api, headers)
    prepared = await api.put('/api/remote-access', headers=headers, json={
        'pairing_enabled': True, 'public_hostname': 'remote.example.com',
        'private_origin': 'https://acc.example.lan', 'certificate_days': 90,
        'machine_identities': not enabled})
    assert prepared.status_code == 200, prepared.text
    original = (await api.get('/api/remote-access')).json()
    draft = {key: original[key] for key in (
        'pairing_enabled', 'public_hostname', 'private_origin', 'certificate_days', 'machine_identities')}
    current = await api.put('/api/remote-access', headers=headers, json={
        **draft, 'machine_identities': enabled, 'expected_revision': original['settings_revision']})
    assert current.status_code == 200, current.text
    stale = await api.put('/api/remote-access', headers=headers, json={
        **draft, 'machine_identities': not enabled, 'expected_revision': original['settings_revision']})
    assert stale.status_code == 409, stale.text
    state = (await api.get('/api/remote-access')).json()
    assert state['machine_identities'] is enabled


async def test_pair_confirmation_download_rename_revoke(api, env, monkeypatch, signer):
    from acc.server.models import Device, DevicePairing
    headers, _ = await setup(api, env, monkeypatch)
    await enable(api, headers)
    pending = await start(api, headers)
    token = pending['link'].split('#')[1]
    assert len(token) >= 43
    # Only hashes persisted; never bundle, password, or capability.
    with api.app.state.db() as s:
        row = s.get(DevicePairing, pending['id'])
        assert row.token_hash != token
    claim = await api.post('/api/device-pairing/claim', headers={'Origin': headers['Origin']}, json={'token': token})
    assert claim.status_code == 200, claim.text
    receipt = claim.json()['receipt']
    assert (await api.post('/api/device-pairing/claim', json={'token': token})).status_code == 410
    assert (await api.post('/api/device-pairing/download', json={'receipt': receipt})).status_code == 409
    confirmed = await api.post(f"/api/devices/pairings/{pending['id']}/confirm", headers=headers, json=await confirmation_body(api, pending))
    assert confirmed.status_code == 200, confirmed.text
    detail = (await api.get(f"/api/devices/pairings/{pending['id']}")).json()
    assert detail['password'] and detail['status'] == 'ready'
    downloaded = await api.post('/api/device-pairing/download', json={'receipt': receipt})
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.headers['cache-control'] == 'no-store'
    assert len(downloaded.content) > 500
    assert (await api.post('/api/device-pairing/download', json={'receipt': receipt})).status_code == 410
    listing = (await api.get('/api/devices')).json()['devices']
    assert len(listing) == 1
    device = listing[0]
    assert 'certificate_pem' not in device and 'password' not in device
    renamed = await api.patch('/api/devices/' + device['id'], headers=headers, json={'label': 'Travel phone'})
    assert renamed.status_code == 200, renamed.text
    revoked = await api.post('/api/devices/' + device['id'] + '/revoke', headers=headers, json={})
    assert revoked.status_code == 200, revoked.text
    with api.app.state.db() as s:
        assert s.get(Device, device['id']).status == 'revoked'
        actions = set(s.scalars(select(AuditLog.action)))
        assert {'device.pair', 'device.rename', 'device.revoke'} <= actions
    assert (await api.get('/api/devices')).status_code == 401  # all person's sessions revoked


async def test_expired_pairing_and_address_limit(api, env, monkeypatch):
    from acc.server.models import DevicePairing
    headers, _ = await setup(api, env, monkeypatch)
    await enable(api, headers)
    pending = await start(api, headers)
    with write_db(api.app.state.db) as s:
        s.get(DevicePairing, pending['id']).expires_at = auth.now() - timedelta(seconds=1)
        s.commit()
    assert (await api.post('/api/device-pairing/claim', json={'token': pending['link'].split('#')[1]})).status_code == 410
    for _ in range(10):
        await api.post('/api/device-pairing/claim', json={'token': 'x' * 43})
    assert (await api.post('/api/device-pairing/claim', json={'token': 'x' * 43})).status_code == 429


async def test_public_host_cannot_pair_even_with_spoofed_private_headers(api, env, monkeypatch):
    headers, _ = await setup(api, env, monkeypatch)
    await enable(api, headers)
    pending = await start(api, headers)
    for host in ('remote.example.com', 'REMOTE.EXAMPLE.COM', 'remote.example.com.'):
        public_headers = {**headers, 'Host': host, 'X-Forwarded-For': '10.0.0.1'}
        result = await api.post('/api/devices/pairings', headers=public_headers, json={'label': 'Phone'})
        assert result.status_code == 403, result.text
        assert (await api.get('/api/devices/pairings', headers=public_headers)).status_code == 403
        assert (await api.get('/api/devices/pairings/' + pending['id'], headers=public_headers)).status_code == 403
    import httpx
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app, client=('203.0.113.5', 1234)),
                                  base_url='http://acc', cookies=api.cookies) as external:
        assert (await external.post('/api/device-pairing/claim', headers={'X-Forwarded-For': '10.0.0.1'},
                                    json={'token': pending['link'].split('#')[1]})).status_code == 403



def test_rendered_registry_revocation_rejected_by_caddy(tmp_path):
    import base64
    import sys
    from pathlib import Path

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.serialization import pkcs12
    from sqlalchemy.orm import sessionmaker
    from test_device_ca import helper

    from acc.server import devices
    from acc.server.db import make_engine, migrate
    from acc.server.models import Device, Setting, User
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
    from remote_access_qualification.harness import Lab
    engine = make_engine('sqlite:///' + str(tmp_path / 'devices.db'))
    migrate(str(engine.url))
    db = sessionmaker(engine)
    with Lab() as lab, db() as s:
        authority = tmp_path / 'authority'
        signer = helper()
        signer.initialize(authority)
        issued = signer.issue(authority, {'operation': 'issue', 'device_id': 'a' * 32, 'days': 90})
        key, cert, _ = pkcs12.load_key_and_certificates(base64.b64decode(issued['pkcs12']), issued['password'].encode())
        pem = issued['certificate']
        (lab.root / 'active.pem').write_text(pem)
        (lab.root / 'active.key').write_bytes(key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        (lab.root / 'device_ca.pem').write_bytes((authority / 'ca.pem').read_bytes())
        s.add(User(username='alex', password_hash='x', totp_secret='x'))  # the gate only admits current people
        row = Device(id='a' * 32, person='alex', label='Phone', kind='browser', status='active',
                     certificate_pem=pem, fingerprint=devices.fingerprint(cert), serial=str(cert.serial_number),
                     expires_at=cert.not_valid_after_utc)
        s.add(row)
        s.add(Setting(key='remote_access', value={'public_hostname': 'remote.example.com'}))
        s.commit()
        def apply():
            lab.reload_fragment(devices.policy_fragment(s, (lab.root / 'device_ca.pem').read_text()))
        apply()
        assert lab.request('active') == 200
        row.status = 'revoked'
        s.commit()
        apply()
        assert lab.request('active') is None
        assert lab.count == 1
    engine.dispose()


async def test_manual_renewal_preserves_device_and_audits(api, env, monkeypatch, signer):
    headers, _ = await setup(api, env, monkeypatch)
    await enable(api, headers)
    async def finish(pending):
        claim = await api.post('/api/device-pairing/claim', json={'token': pending['link'].split('#')[1]})
        assert claim.status_code == 200
        result = await api.post(f"/api/devices/pairings/{pending['id']}/confirm", headers=headers, json=await confirmation_body(api, pending))
        assert result.status_code == 200, result.text
        assert (await api.post('/api/device-pairing/download', json={'receipt': claim.json()['receipt']})).status_code == 200
    await finish(await start(api, headers))
    first = (await api.get('/api/devices')).json()['devices'][0]
    renewed = await api.post('/api/devices/' + first['id'] + '/renew', headers=headers, json={})
    assert renewed.status_code == 200, renewed.text
    await finish(renewed.json())
    rows = (await api.get('/api/devices')).json()['devices']
    assert len(rows) == 1 and rows[0]['id'] == first['id']
    assert rows[0]['fingerprint'] != first['fingerprint']
    with api.app.state.db() as s:
        assert s.scalar(select(AuditLog).where(AuditLog.action == 'device.renew')).target == first['id']


async def test_members_only_see_their_devices_and_owner_must_confirm(api, env, monkeypatch, signer, member_accounts):
    from helpers import verified_post
    from test_users import browser, csrf, join

    from acc.server.models import AuthSession
    headers, _ = await setup(api, env, monkeypatch)
    await enable(api, headers)
    invited = await verified_post(api, '/api/users/invites', {'role': 'member', 'account': 'dana-agent'})
    assert invited.status_code == 201, invited.text
    async with browser(api) as member:
        assert (await join(member, invited.json()['link'], 'alex')).status_code == 200
        with write_db(api.app.state.db) as s:
            s.get(AuthSession, auth.digest(member.cookies.get('acc_session'))).reauthenticated_at = auth.now()
            s.commit()
        assert (await member.get('/api/remote-access')).status_code == 403
        pending = await start(member, csrf(member))
        claim = await member.post('/api/device-pairing/claim', json={'token': pending['link'].split('#')[1]})
        assert claim.status_code == 200
        assert (await member.post(f"/api/devices/pairings/{pending['id']}/confirm", headers=csrf(member), json={})).status_code == 403
        confirmed = await api.post(f"/api/devices/pairings/{pending['id']}/confirm", headers=headers, json=await confirmation_body(api, pending))
        assert confirmed.status_code == 200, confirmed.text
        member_devices = (await member.get('/api/devices')).json()['devices']
        assert len(member_devices) == 1 and member_devices[0]['person'] == 'alex'
        own = await start(api, headers)
        assert (await member.get('/api/devices/pairings/' + own['id'])).status_code == 404
        assert (await api.get('/api/devices/pairings/' + pending['id'])).json()['password']
        users = (await api.get('/api/users')).json()['users']
        member_id = next(u['id'] for u in users if u['username'] == 'alex')
        removed = await api.post(f'/api/users/{member_id}/remove', headers=headers, json={'resources': 'transfer'})
        assert removed.status_code == 200, removed.text
        from acc.server.models import Device
        with api.app.state.db() as s:
            assert s.get(Device, member_devices[0]['id']).status == 'revoked'
        assert (await api.post('/api/device-pairing/download', json={'receipt': claim.json()['receipt']})).status_code == 410


def test_transient_import_is_destroyed_on_timeout(monkeypatch):
    import time

    from acc.server import devices
    monkeypatch.setattr(devices, 'IMPORT_SECONDS', 0.02)
    store = devices.Imports()
    store.put('id', {'pkcs12': 'secret'})
    deadline = time.monotonic() + 1
    while store.get('id') and time.monotonic() < deadline:
        time.sleep(.01)
    assert store.pop('id') is None


def test_apple_identity_profile_keeps_password_out_of_download():
    import plistlib

    from acc.server import devices
    raw = devices.mobileconfig(b'ENCRYPTED-PKCS12')
    profile = plistlib.loads(raw)
    identity = profile['PayloadContent'][0]
    assert identity['PayloadType'] == 'com.apple.security.pkcs12'
    assert identity['PayloadContent'] == b'ENCRYPTED-PKCS12'
    assert 'Password' not in identity
    assert profile['PayloadRemovalDisallowed'] is False


async def test_racing_receivers_get_only_one_identity(api, env, monkeypatch, signer, backend_url):
    import asyncio
    await pause_supervisor(api)
    headers, _ = await setup(api, env, monkeypatch)
    await enable(api, headers)
    pending = await start(api, headers)
    token = pending['link'].split('#')[1]
    with guard_database(api, monkeypatch) as db:
        with transaction_rendezvous(db, 'UPDATE device_pairing'):
            claims = await asyncio.gather(*[
                api.post('/api/device-pairing/claim', json={'token': token}) for _ in range(2)])
        assert sorted(r.status_code for r in claims) == [200, 410]
        receipt = next(r.json()['receipt'] for r in claims if r.status_code == 200)
        body = await confirmation_body(api, pending)
        with transaction_rendezvous(db, 'UPDATE device_pairing'):
            confirmations = await asyncio.gather(*[
                api.post(f"/api/devices/pairings/{pending['id']}/confirm", headers=headers, json=body) for _ in range(2)])
        assert sorted(r.status_code for r in confirmations) == [200, 409]
        with transaction_rendezvous(db, 'SELECT device_pairing'):
            downloads = await asyncio.gather(*[
                api.post('/api/device-pairing/download', json={'receipt': receipt}) for _ in range(2)])
        assert sorted(r.status_code for r in downloads) == [200, 410]
    assert len((await api.get('/api/devices')).json()['devices']) == 1


async def test_pairing_requires_recent_human_verification(api, env, monkeypatch):
    from acc.server.models import AuthSession
    headers, _ = await setup(api, env, monkeypatch)
    await enable(api, headers)
    with write_db(api.app.state.db) as s:
        s.get(AuthSession, auth.digest(api.cookies.get('acc_session'))).reauthenticated_at = auth.now() - timedelta(minutes=6)
        s.commit()
    assert (await api.post('/api/devices/pairings', headers=headers, json={'label': 'Phone'})).status_code == 403


async def test_expired_identity_is_not_reported_active_or_rendered(api, env, monkeypatch, signer, tmp_path):
    from acc.server import devices
    from acc.server.models import Device
    headers, _ = await setup(api, env, monkeypatch)
    await enable(api, headers)
    pending = await start(api, headers)
    assert (await api.post('/api/device-pairing/claim', json={'token': pending['link'].split('#')[1]})).status_code == 200
    assert (await api.post(f"/api/devices/pairings/{pending['id']}/confirm", headers=headers, json=await confirmation_body(api, pending))).status_code == 200
    id = (await api.get('/api/devices')).json()['devices'][0]['id']
    with write_db(api.app.state.db) as s:
        s.get(Device, id).expires_at = auth.now() - timedelta(seconds=1)
        s.commit()
    assert (await api.get('/api/devices')).json()['devices'][0]['status'] == 'expired'

    with api.app.state.db() as s:
        rendered = devices.client_authentication(s, (tmp_path / 'ca.pem').read_text())
        assert rendered['verifiers'] == [{'verifier': 'leaf', 'TrustedLeafCerts': []}]


async def test_concurrent_claims_issue_exactly_one_receipt(api, env, monkeypatch, backend_url):
    import asyncio

    from acc.server.models import DevicePairing
    await pause_supervisor(api)
    headers, _ = await setup(api, env, monkeypatch)
    await enable(api, headers)
    pending = await start(api, headers)
    token = pending['link'].split('#')[1]
    with guard_database(api, monkeypatch) as db, transaction_rendezvous(db, 'UPDATE device_pairing'):
        claims = await asyncio.gather(*[api.post('/api/device-pairing/claim', json={'token': token}) for _ in range(2)])
    assert sorted(r.status_code for r in claims) == [200, 410], [r.text for r in claims]
    receipt = next(r.json()['receipt'] for r in claims if r.status_code == 200)
    with api.app.state.db() as s:
        assert s.get(DevicePairing, pending['id']).receipt_hash == auth.digest(receipt)


async def test_concurrent_confirmations_sign_once(api, env, monkeypatch, signer, backend_url):
    import asyncio

    from acc.server import devices
    from acc.server.models import AuditLog, Device
    await pause_supervisor(api)
    headers, _ = await setup(api, env, monkeypatch)
    await enable(api, headers)
    pending = await start(api, headers)
    assert (await api.post('/api/device-pairing/claim', json={'token': pending['link'].split('#')[1]})).status_code == 200
    calls = []
    sign = devices.sign
    monkeypatch.setattr(devices, 'sign', lambda body: calls.append(body) or sign(body))
    body = await confirmation_body(api, pending)
    with guard_database(api, monkeypatch) as db, transaction_rendezvous(db, 'UPDATE device_pairing'):
        confirmations = await asyncio.gather(*[
            api.post(f"/api/devices/pairings/{pending['id']}/confirm", headers=headers, json=body) for _ in range(2)])
    assert sorted(r.status_code for r in confirmations) == [200, 409], [r.text for r in confirmations]
    assert len(calls) == 1
    with api.app.state.db() as s:
        assert len(list(s.scalars(select(Device)))) == 1
        assert len(list(s.scalars(select(AuditLog).where(AuditLog.action == 'device.pair')))) == 1


async def test_signer_call_does_not_hold_the_database_write_lock(api, env, monkeypatch, signer):
    import sqlite3

    from acc.server import devices
    headers, _ = await setup(api, env, monkeypatch)
    await enable(api, headers)
    pending = await start(api, headers)
    assert (await api.post('/api/device-pairing/claim', json={'token': pending['link'].split('#')[1]})).status_code == 200
    path = api.app.state.db.kw['bind'].url.database
    seen = []
    sign = devices.sign

    def probe(body):
        # A hung signer (up to its 15 s deadline) must not freeze every other ACC write.
        other = sqlite3.connect(path, timeout=0.2, isolation_level=None)
        try:
            other.execute('BEGIN IMMEDIATE')
            other.execute('ROLLBACK')
            seen.append('free')
        except sqlite3.OperationalError:
            seen.append('locked')
        finally:
            other.close()
        return sign(body)
    monkeypatch.setattr(devices, 'sign', probe)
    assert (await api.post(f"/api/devices/pairings/{pending['id']}/confirm", headers=headers, json=await confirmation_body(api, pending))).status_code == 200
    assert seen == ['free']


async def test_failed_signing_leaves_the_claim_confirmable(api, env, monkeypatch):
    from acc.server import devices
    headers, _ = await setup(api, env, monkeypatch)
    await enable(api, headers)
    pending = await start(api, headers)
    assert (await api.post('/api/device-pairing/claim', json={'token': pending['link'].split('#')[1]})).status_code == 200
    for bad in ([], {'certificate': 7}, OSError('down')):
        def fail(body, bad=bad):
            if isinstance(bad, Exception):
                raise bad
            return bad
        monkeypatch.setattr(devices, 'sign', fail)
        result = await api.post(f"/api/devices/pairings/{pending['id']}/confirm", headers=headers, json=await confirmation_body(api, pending))
        assert result.status_code == 503, result.text
    listed = (await api.get('/api/devices/pairings')).json()['pairings']
    assert [p['id'] for p in listed] == [pending['id']]


def test_policy_excludes_devices_of_people_without_an_account(tmp_path):
    from sqlalchemy.orm import sessionmaker
    from test_device_ca import helper

    from acc.server import devices
    from acc.server.db import make_engine, migrate
    from acc.server.models import Device, User
    engine = make_engine('sqlite:///' + str(tmp_path / 'devices.db'))
    migrate(str(engine.url))
    ca = helper()
    ca.initialize(tmp_path / 'authority')
    with sessionmaker(engine)() as s:
        s.add(User(username='alex', password_hash='x', totp_secret='x'))
        for person, id in (('alex', 'a' * 32), ('gone', 'b' * 32)):
            result = ca.issue(tmp_path / 'authority', {'operation': 'issue', 'device_id': id, 'days': 30})
            cert = devices.identity(result, id, 30)
            s.add(Device(id=id, person=person, label='Phone', kind='browser', status='active',
                         certificate_pem=result['certificate'], fingerprint=devices.fingerprint(cert),
                         serial=str(cert.serial_number), expires_at=cert.not_valid_after_utc))
        s.commit()
        rendered = devices.client_authentication(s, (tmp_path / 'authority' / 'ca.pem').read_text())
        assert len(rendered['verifiers'][0]['TrustedLeafCerts']) == 1


@pytest.mark.parametrize('label', ['‮enohp', 'Phone\nalex', 'Phone​', 'Tab\there', ' '])
async def test_device_names_reject_control_and_invisible_characters(api, env, monkeypatch, label):
    headers, _ = await setup(api, env, monkeypatch)
    await enable(api, headers)
    result = await api.post('/api/devices/pairings', headers=headers, json={'label': label})
    assert result.status_code == 422, result.text
    assert (await api.post('/api/devices/pairings', headers=headers, json={'label': 'Alex’s Pixel 8 · work'})).status_code == 200


async def test_public_host_is_refused_before_pairing_or_device_lookup(api, env, monkeypatch, signer):
    headers, _ = await setup(api, env, monkeypatch)
    await enable(api, headers)
    public_headers = {**headers, 'Host': 'remote.example.com'}
    # No existence oracle for pairing or device ids from the public hostname.
    for path in ('/api/devices/pairings/' + '0' * 32,):
        assert (await api.get(path, headers=public_headers)).status_code == 403
    assert (await api.post('/api/devices/' + '0' * 32 + '/renew', headers=public_headers, json={})).status_code == 403


async def test_rename_audit_names_person_and_label(api, env, monkeypatch, signer):
    headers, _ = await setup(api, env, monkeypatch)
    await enable(api, headers)
    pending = await start(api, headers)
    assert (await api.post('/api/device-pairing/claim', json={'token': pending['link'].split('#')[1]})).status_code == 200
    assert (await api.post(f"/api/devices/pairings/{pending['id']}/confirm", headers=headers, json=await confirmation_body(api, pending))).status_code == 200
    device = (await api.get('/api/devices')).json()['devices'][0]
    assert (await api.patch('/api/devices/' + device['id'], headers=headers, json={'label': 'Travel phone'})).status_code == 200
    with api.app.state.db() as s:
        entry = s.scalar(select(AuditLog).where(AuditLog.action == 'device.rename'))
        assert entry.detail == {'person': device['person'], 'from': 'Alex phone', 'to': 'Travel phone'}


async def test_policy_failure_during_signing_discards_identity(api, env, monkeypatch, signer):
    from acc.server import devices
    from acc.server.models import Device, DevicePairing, Setting
    headers, _ = await setup(api, env, monkeypatch)
    await enable(api, headers)
    pending = await start(api, headers)
    assert (await api.post('/api/device-pairing/claim', json={'token': pending['link'].split('#')[1]})).status_code == 200
    body = await confirmation_body(api, pending)
    original = devices.sign
    def interrupted(message):
        result = original(message)
        # The signer runs outside the DB write transaction. An edge failure can
        # become durable while its result is in flight.
        with write_db(api.app.state.db) as s:
            s.merge(Setting(key='remote_access_runtime', value={'pending': True, 'desired_enabled': False}))
            s.commit()
        return result
    monkeypatch.setattr(devices, 'sign', interrupted)
    result = await api.post('/api/devices/pairings/' + pending['id'] + '/confirm', headers=headers, json=body)
    assert result.status_code == 409, result.text
    with api.app.state.db() as s:
        assert list(s.scalars(select(Device))) == []
        assert s.get(DevicePairing, pending['id']).status == 'cancelled'
    assert api.app.state.ctx.device_imports.get(pending['id']) is None


async def test_device_removed_during_renewal_is_not_resurrected(api, env, monkeypatch, signer):
    from acc.server import devices
    from acc.server.models import Device, DevicePairing
    headers, _ = await setup(api, env, monkeypatch)
    await enable(api, headers)
    initial = await start(api, headers)
    assert (await api.post('/api/device-pairing/claim', json={'token': initial['link'].split('#')[1]})).status_code == 200
    assert (await api.post('/api/devices/pairings/' + initial['id'] + '/confirm', headers=headers,
                          json=await confirmation_body(api, initial))).status_code == 200
    device_id = (await api.get('/api/devices')).json()['devices'][0]['id']
    pending = (await api.post('/api/devices/' + device_id + '/renew', headers=headers, json={})).json()
    assert (await api.post('/api/device-pairing/claim', json={'token': pending['link'].split('#')[1]})).status_code == 200
    body = await confirmation_body(api, pending)
    original = devices.sign
    def removed(message):
        result = original(message)
        with write_db(api.app.state.db) as s:
            s.delete(s.get(Device, device_id))
            for row in s.scalars(select(DevicePairing).where(DevicePairing.device_id == device_id)):
                row.status = 'cancelled'
            s.get(DevicePairing, pending['id']).status = 'cancelled'
            s.commit()
        return result
    monkeypatch.setattr(devices, 'sign', removed)
    result = await api.post('/api/devices/pairings/' + pending['id'] + '/confirm', headers=headers, json=body)
    assert result.status_code in (409, 410), result.text
    with api.app.state.db() as s:
        assert s.get(Device, device_id) is None
    assert api.app.state.ctx.device_imports.get(pending['id']) is None
