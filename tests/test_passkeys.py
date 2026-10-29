"""Passkey boundaries exercised with a software ES256 authenticator (none attestation)."""
import base64
import hashlib
import json
import struct

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from helpers import TOTP_AT, write_db
from sqlalchemy import select
from test_auth import PASSWORD, cookie_client, do_setup

from acc.server import auth
from acc.server.models import AuditLog

ORIGIN = 'http://127.0.0.1:18740'


def b64(data):
    return base64.urlsafe_b64encode(data).decode().rstrip('=')


class Authenticator:
    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.id = self.key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)[-32:]
        self.count = 0

    def credential(self, options, *, origin=ORIGIN, uv=True, registration=False, handle=None, cross_origin=False):
        import cbor2
        client = json.dumps({'type': 'webauthn.create' if registration else 'webauthn.get',
                             'challenge': options['challenge'], 'origin': origin, 'crossOrigin': cross_origin}).encode()
        rp = options.get('rp', {}).get('id', options.get('rpId'))
        self.count += 1
        data = hashlib.sha256(rp.encode()).digest() + bytes([1 | (4 if uv else 0) | (64 if registration else 0)]) + struct.pack('>I', self.count)
        response = {'clientDataJSON': b64(client)}
        if registration:
            numbers = self.key.public_key().public_numbers()
            cose = {1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, 'big'), -3: numbers.y.to_bytes(32, 'big')}
            data += bytes(16) + struct.pack('>H', len(self.id)) + self.id + cbor2.dumps(cose)
            response['attestationObject'] = b64(cbor2.dumps({'fmt': 'none', 'attStmt': {}, 'authData': data}))
        else:
            response.update(authenticatorData=b64(data), signature=b64(self.key.sign(data + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))), userHandle=handle)
        return {'id': b64(self.id), 'rawId': b64(self.id), 'type': 'public-key', 'response': response}


async def setup(api, env, monkeypatch):
    secret = await do_setup(api, env)
    cookie_client(api)
    monkeypatch.setattr(auth.time, 'time', lambda: TOTP_AT + 30)
    headers = {'Origin': ORIGIN, 'X-ACC-CSRF': api.cookies.get('acc_csrf')}
    reauth = await api.post('/api/auth/passkeys/reauth', headers=headers,
                            json={'password': PASSWORD, 'code': auth.totp(secret, TOTP_AT + 30)})
    assert reauth.status_code == 200, reauth.text
    return headers, secret


async def enroll(api, headers, authenticator, name='Laptop'):
    begin = await api.post('/api/auth/passkeys/register/begin', headers=headers, json={})
    assert begin.status_code == 200, begin.text
    options = begin.json()['options']
    assert options['authenticatorSelection']['residentKey'] == 'required'
    assert options['authenticatorSelection']['userVerification'] == 'required'
    done = await api.post('/api/auth/passkeys/register/complete', headers=headers, json={
        'challenge_id': begin.json()['challenge_id'], 'name': name,
        'credential': authenticator.credential(options, registration=True, origin=headers['Origin'])})
    assert done.status_code == 200, done.text
    return done.json(), options['user']['id']


async def test_registration_requires_fresh_reauthentication(api, env):
    await do_setup(api, env)
    cookie_client(api)
    result = await api.post('/api/auth/passkeys/register/begin', headers={
        'Origin': ORIGIN, 'X-ACC-CSRF': api.cookies.get('acc_csrf')}, json={})
    assert result.status_code == 403


async def test_discoverable_signin_replay_and_remove(api, env, monkeypatch):
    headers, _ = await setup(api, env, monkeypatch)
    device = Authenticator()
    registered, handle = await enroll(api, headers, device)
    listing = (await api.get('/api/auth/passkeys')).json()
    assert listing['passkeys'][0]['name'] == 'Laptop'
    assert 'public_key' not in listing['passkeys'][0]
    api.cookies.clear()
    begin = await api.post('/api/auth/passkeys/login/begin', headers={'Origin': ORIGIN}, json={})
    assert begin.status_code == 200
    assert not begin.json()['options'].get('allowCredentials')
    body = {'challenge_id': begin.json()['challenge_id'], 'credential': device.credential(begin.json()['options'], handle=handle)}
    done = await api.post('/api/auth/passkeys/login/complete', headers={'Origin': ORIGIN}, json=body)
    assert done.status_code == 200, done.text
    assert (await api.get('/api/auth/state')).json()['authenticated']
    replay = await api.post('/api/auth/passkeys/login/complete', headers={'Origin': ORIGIN}, json=body)
    assert replay.status_code == 401
    headers['X-ACC-CSRF'] = api.cookies.get('acc_csrf')
    # Passkey sign-in is recent two-factor reauthentication.
    removed = await api.post(f"/api/auth/passkeys/{registered['id']}/remove", headers=headers, json={})
    assert removed.status_code == 200
    assert not (await api.get('/api/auth/passkeys')).json()['passkeys']
    with api.app.state.db() as s:
        actions = set(s.scalars(select(AuditLog.action)))
    assert {'auth.passkey_registered', 'auth.passkey_login', 'auth.passkey_failed', 'auth.passkey_removed'} <= actions


@pytest.mark.parametrize("changes", [{"origin": "https://evil.example.com"}, {"uv": False}, {"handle": b64(b"other-user")}])
async def test_origin_uv_and_handle_are_verified(api, env, monkeypatch, changes):
    headers, _ = await setup(api, env, monkeypatch)
    device = Authenticator()
    _, handle = await enroll(api, headers, device)
    begin = (await api.post('/api/auth/passkeys/login/begin', headers=headers, json={})).json()
    credential = device.credential(begin['options'], **{'handle': handle, **changes})
    result = await api.post('/api/auth/passkeys/login/complete', headers=headers,
                            json={'challenge_id': begin['challenge_id'], 'credential': credential})
    assert result.status_code == 401, result.text
    bad = await api.post('/api/auth/passkeys/login/begin', headers={'Origin': 'https://evil.example.com'}, json={})
    assert bad.status_code == 403


async def test_policy_guards_and_one_use_owner_recovery(api, env, monkeypatch):
    headers, secret = await setup(api, env, monkeypatch)
    policy = {'owners': 'require', 'members': 'allow'}
    assert (await api.post('/api/auth/passkeys/policy', headers=headers, json=policy)).status_code == 409
    first = Authenticator()
    _, handle = await enroll(api, headers, first, 'First')
    await enroll(api, headers, Authenticator(), 'Second')
    assert (await api.post('/api/auth/passkeys/policy', headers=headers, json=policy)).status_code == 409
    code = (await api.post('/api/auth/passkeys/recovery-code', headers=headers, json={})).json()['code']
    assert (await api.post('/api/auth/passkeys/policy', headers=headers, json=policy)).status_code == 200
    begin = (await api.post('/api/auth/passkeys/verify/begin', headers=headers, json={})).json()
    verified = await api.post('/api/auth/passkeys/verify/complete', headers=headers, json={
        'challenge_id': begin['challenge_id'], 'credential': first.credential(begin['options'], handle=handle)})
    assert verified.status_code == 200, verified.text
    key_id = (await api.get('/api/auth/passkeys')).json()['passkeys'][0]['id']
    assert (await api.post(f'/api/auth/passkeys/{key_id}/remove', headers=headers, json={})).status_code == 409
    api.cookies.clear()
    monkeypatch.setattr(auth.time, 'time', lambda: TOTP_AT + 60)
    login = await api.post('/api/auth/login', headers={'Origin': ORIGIN}, json={
        'username': 'casey', 'password': PASSWORD, 'code': auth.totp(secret, TOTP_AT + 60)})
    assert login.status_code == 403
    recover = {'username': 'casey', 'password': PASSWORD, 'code': code}
    result = await api.post('/api/auth/passkeys/recover', headers={'Origin': ORIGIN}, json=recover)
    assert result.status_code == 200, result.text
    assert (await api.get('/api/auth/passkeys')).json()['policy']['owners'] == 'allow'
    assert (await api.post('/api/auth/passkeys/recover', headers={'Origin': ORIGIN}, json=recover)).status_code == 401


async def test_passkey_begins_share_login_address_throttle(api, env):
    cookie_client(api)
    for _ in range(5):
        assert (await api.post('/api/auth/passkeys/login/begin', headers={'Origin': ORIGIN}, json={})).status_code == 200
    assert (await api.post('/api/auth/passkeys/login/begin', headers={'Origin': ORIGIN}, json={})).status_code == 429


async def test_browser_binding_counter_and_registration_replay(api, env, monkeypatch):
    headers, _ = await setup(api, env, monkeypatch)
    device = Authenticator()
    registered, handle = await enroll(api, headers, device)
    begin = (await api.post('/api/auth/passkeys/login/begin', headers=headers, json={})).json()
    body = {'challenge_id': begin['challenge_id'], 'credential': device.credential(begin['options'], handle=handle)}
    # Swapping the challenge cookie must fail even with a valid signed assertion.
    api.cookies.set('acc_webauthn', 'different-browser')
    assert (await api.post('/api/auth/passkeys/login/complete', headers=headers, json=body)).status_code == 401
    api.cookies.clear()
    begin = (await api.post('/api/auth/passkeys/login/begin', headers={'Origin': ORIGIN}, json={})).json()
    device.count = 0  # registration stored 1: a nonzero counter may not regress
    body = {'challenge_id': begin['challenge_id'], 'credential': device.credential(begin['options'], handle=handle)}
    assert (await api.post('/api/auth/passkeys/login/complete', headers={'Origin': ORIGIN}, json=body)).status_code == 401


async def test_private_ca_rp_ignores_forwarded_host_and_rejects_insecure_origin(api, env, monkeypatch):
    from dataclasses import replace
    origins = ('https://acc.example.lan', 'https://acc.example.com', 'http://acc')
    monkeypatch.setattr(api.app.state.ctx, 'settings', replace(env['settings'], allowed_origins=origins))
    for origin, rp in [('https://acc.example.lan', 'acc.example.lan'), ('https://acc.example.com', 'acc.example.com')]:
        result = await api.post('/api/auth/passkeys/login/begin', headers={
            'Origin': origin, 'X-Forwarded-Host': 'evil.example.com', 'X-Forwarded-Proto': 'http'}, json={})
        assert result.status_code == 200
        assert result.json()['options']['rpId'] == rp
    for headers in ({'Origin': 'http://acc'}, {}, {'Origin': 'https://evil.example.com'}):
        assert (await api.post('/api/auth/passkeys/login/begin', headers=headers, json={})).status_code == 403


async def test_reauthentication_expires_and_cannot_be_shared_between_sessions(api, env, monkeypatch):
    from datetime import timedelta

    from acc.server.models import AuthSession
    headers, _ = await setup(api, env, monkeypatch)
    with write_db(api.app.state.db) as s:
        session = s.get(AuthSession, auth.digest(api.cookies.get('acc_session')))
        session.reauthenticated_at = auth.now() - timedelta(minutes=6)
        s.commit()
    assert (await api.post('/api/auth/passkeys/register/begin', headers=headers, json={})).status_code == 403


async def test_member_cannot_manage_owner_passkeys_or_policy(api, env, monkeypatch, member_accounts):
    from test_users import browser, csrf, join
    headers, _ = await setup(api, env, monkeypatch)
    registered, _ = await enroll(api, headers, Authenticator())
    link = (await api.post('/api/users/invites', headers=headers, json={'role': 'member', 'account': 'dana-agent'})).json()['link']
    async with browser(api) as member:
        await join(member, link, 'alex')
        assert not (await member.get('/api/auth/passkeys')).json()['passkeys']
        result = await member.post(f"/api/auth/passkeys/{registered['id']}/name", headers=csrf(member), json={'name': 'stolen'})
        assert result.status_code == 404
        assert (await member.post(f"/api/auth/passkeys/{registered['id']}/remove", headers=csrf(member), json={})).status_code == 404
        assert (await member.post('/api/auth/passkeys/policy', headers=csrf(member), json={'owners': 'allow', 'members': 'require'})).status_code == 403


async def test_cross_origin_signed_ceremony_is_refused(api, env, monkeypatch):
    headers, _ = await setup(api, env, monkeypatch)
    device = Authenticator()
    _, handle = await enroll(api, headers, device)
    begin = (await api.post('/api/auth/passkeys/login/begin', headers=headers, json={})).json()
    credential = device.credential(begin['options'], handle=handle, cross_origin=True)
    result = await api.post('/api/auth/passkeys/login/complete', headers=headers, json={
        'challenge_id': begin['challenge_id'], 'credential': credential})
    assert result.status_code == 401


async def test_invite_reset_revokes_passkeys_and_cannot_bypass_require(api, env, monkeypatch):
    from test_users import browser, join
    headers, _ = await setup(api, env, monkeypatch)
    await enroll(api, headers, Authenticator())
    uid = (await api.get('/api/users')).json()['users'][0]['id']
    link = (await api.post(f'/api/users/{uid}/reset', headers=headers, json={})).json()['link']
    async with browser(api) as other:
        await join(other, link, 'casey')
        assert not (await other.get('/api/auth/passkeys')).json()['passkeys']


async def test_role_promotion_cannot_create_unrecoverable_required_owner(api, env, monkeypatch, member_accounts):
    from test_users import browser, join

    from acc.server.models import PasskeyPolicy
    headers, _ = await setup(api, env, monkeypatch)
    link = (await api.post('/api/users/invites', headers=headers, json={'role': 'member', 'account': 'dana-agent'})).json()['link']
    async with browser(api) as other:
        await join(other, link, 'alex')
    uid = next(u['id'] for u in (await api.get('/api/users')).json()['users'] if u['username'] == 'alex')
    with write_db(api.app.state.db) as s:
        s.add(PasskeyPolicy(id=1, owners='require', members='allow'))
        s.commit()
    result = await api.post(f'/api/users/{uid}/role', headers=headers, json={'role': 'owner'})
    assert result.status_code == 409


async def test_zero_counter_synced_passkeys_can_sign_in_repeatedly(api, env, monkeypatch):
    headers, _ = await setup(api, env, monkeypatch)
    device = Authenticator()
    device.count = -1
    _, handle = await enroll(api, headers, device)
    for _ in range(2):
        begin = (await api.post('/api/auth/passkeys/login/begin', headers=headers, json={})).json()
        device.count = -1
        result = await api.post('/api/auth/passkeys/login/complete', headers=headers, json={
            'challenge_id': begin['challenge_id'], 'credential': device.credential(begin['options'], handle=handle)})
        assert result.status_code == 200, result.text
        headers['X-ACC-CSRF'] = api.cookies.get('acc_csrf')


@pytest.mark.parametrize('invalid', ['expired', 'signature', 'challenge'])
async def test_invalid_assertions_never_create_sessions(api, env, monkeypatch, invalid):
    from datetime import timedelta

    from acc.server.models import PasskeyChallenge
    headers, _ = await setup(api, env, monkeypatch)
    device = Authenticator()
    _, handle = await enroll(api, headers, device)
    api.cookies.clear()
    begin = (await api.post('/api/auth/passkeys/login/begin', headers={'Origin': ORIGIN}, json={})).json()
    options = begin['options']
    if invalid == 'expired':
        with write_db(api.app.state.db) as s:
            s.get(PasskeyChallenge, auth.digest(begin['challenge_id'])).expires_at = auth.now() - timedelta(seconds=1)
            s.commit()
    elif invalid == 'signature':
        device.key = ec.generate_private_key(ec.SECP256R1())
    else:
        options['challenge'] = b64(b'wrong challenge')
    result = await api.post('/api/auth/passkeys/login/complete', headers={'Origin': ORIGIN}, json={
        'challenge_id': begin['challenge_id'], 'credential': device.credential(options, handle=handle)})
    assert result.status_code == 401
    assert not (await api.get('/api/auth/state')).json()['authenticated']


async def test_insecure_configured_origin_is_audited_without_trusting_the_actor(api, env):
    result = await api.post('/api/auth/passkeys/login/begin', headers={'Origin': 'http://acc'}, json={})
    assert result.status_code == 403
    with api.app.state.db() as s:
        assert 'auth.passkey_failed' in set(s.scalars(select(AuditLog.action)))


async def test_registration_failures_are_rate_limited(api, env, monkeypatch):
    headers, _ = await setup(api, env, monkeypatch)
    for _ in range(5):
        begin = await api.post('/api/auth/passkeys/register/begin', headers=headers, json={})
        assert begin.status_code == 200
        bad = await api.post('/api/auth/passkeys/register/complete', headers=headers, json={
            'challenge_id': begin.json()['challenge_id'], 'credential': {}})
        assert bad.status_code == 401
    assert (await api.post('/api/auth/passkeys/register/begin', headers=headers, json={})).status_code == 429


@pytest.mark.parametrize('endpoint', ['reauth', 'recover'])
async def test_slow_password_verification_does_not_block_other_database_writes(api, env, monkeypatch, endpoint):
    import asyncio
    import threading
    headers, secret = await setup(api, env, monkeypatch)
    code = (await api.post('/api/auth/passkeys/recovery-code', headers=headers, json={})).json()['code']
    entered, release, committed = threading.Event(), threading.Event(), threading.Event()
    real_check = auth.password_ok

    def slow_check(stored, password):
        entered.set()
        release.wait(5)
        return real_check(stored, password)

    def other_write():
        with write_db(api.app.state.db) as s:
            s.add(AuditLog(actor='test', action='test.concurrent_write', target='test'))
            s.commit()
        committed.set()

    monkeypatch.setattr(auth, 'password_ok', slow_check)
    monkeypatch.setattr(auth.time, 'time', lambda: TOTP_AT + 60)
    body = {'username': 'casey', 'password': PASSWORD, 'code': code if endpoint == 'recover' else auth.totp(secret, TOTP_AT + 60)}
    request = asyncio.create_task(api.post('/api/auth/passkeys/' + endpoint, headers=headers, json=body))
    assert await asyncio.to_thread(entered.wait, 3)
    writer = asyncio.create_task(asyncio.to_thread(other_write))
    try:
        assert await asyncio.to_thread(committed.wait, 1), 'Password hashing held the database write lock'
    finally:
        release.set()
        result = await request
        await writer
    assert result.status_code == 200


async def test_correct_offline_recovery_survives_account_throttle_from_other_addresses(api, env, monkeypatch):
    headers, _ = await setup(api, env, monkeypatch)
    code = (await api.post('/api/auth/passkeys/recovery-code', headers=headers, json={})).json()['code']
    for _ in range(10):
        api.app.state.ctx.throttle.fail('user:casey')
    api.cookies.clear()
    result = await api.post('/api/auth/passkeys/recover', headers={'Origin': ORIGIN}, json={
        'username': 'casey', 'password': PASSWORD, 'code': code})
    assert result.status_code == 200


async def test_discoverable_failures_count_per_account_across_addresses(api, env, monkeypatch):
    import httpx
    headers, _ = await setup(api, env, monkeypatch)
    device = Authenticator()
    _, handle = await enroll(api, headers, device)
    real_key = device.key
    for n in range(12):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app, client=(f'192.0.2.{n + 1}', 1234)), base_url='http://acc') as client:
            begin = (await client.post('/api/auth/passkeys/login/begin', headers={'Origin': ORIGIN}, json={})).json()
            device.key = real_key if n == 11 else ec.generate_private_key(ec.SECP256R1())
            response = await client.post('/api/auth/passkeys/login/complete', headers={'Origin': ORIGIN}, json={
                'challenge_id': begin['challenge_id'], 'credential': device.credential(begin['options'], handle=handle)})
            assert response.status_code == (200 if n == 11 else 429 if n == 10 else 401), response.text



@pytest.mark.parametrize('address', ['https://acc.example.lan', 'https://acc.example.com'])
async def test_https_public_and_private_ca_origins_verify_signed_credentials_behind_proxy(api, env, monkeypatch, address):
    from dataclasses import replace
    headers, _ = await setup(api, env, monkeypatch)
    ctx = api.app.state.ctx
    monkeypatch.setattr(ctx, 'settings', replace(ctx.settings, allowed_origins=(*ctx.settings.allowed_origins, address)))
    headers = {**headers, 'Origin': address, 'X-Forwarded-Host': 'evil.example.com', 'X-Forwarded-Proto': 'http'}
    device = Authenticator()
    _, handle = await enroll(api, headers, device)
    begin = (await api.post('/api/auth/passkeys/login/begin', headers=headers, json={})).json()
    assert begin['options']['rpId'] == address.removeprefix('https://')
    result = await api.post('/api/auth/passkeys/login/complete', headers=headers, json={
        'challenge_id': begin['challenge_id'], 'credential': device.credential(begin['options'], handle=handle, origin=address)})
    assert result.status_code == 200, result.text


async def test_member_recovery_cannot_relax_the_owners_policy_for_other_members(api, env, monkeypatch, member_accounts):
    from test_users import browser, join

    from acc.server.models import User
    owner_headers, _ = await setup(api, env, monkeypatch)
    link = (await api.post('/api/users/invites', headers=owner_headers,
                           json={'role': 'member', 'account': 'dana-agent'})).json()['link']
    async with browser(api) as member:
        await join(member, link, 'alex')
        headers = {'Origin': ORIGIN, 'X-ACC-CSRF': member.cookies.get('acc_csrf')}
        with api.app.state.db() as s:
            secret = s.scalar(select(User).where(User.username == 'alex')).totp_secret
        verified = await member.post('/api/auth/passkeys/reauth', headers=headers, json={
            'password': 'member password 123', 'code': auth.totp(secret, TOTP_AT + 30)})
        assert verified.status_code == 200, verified.text
        await enroll(member, headers, Authenticator(), 'Member key')
        code = (await member.post('/api/auth/passkeys/recovery-code', headers=headers, json={})).json()['code']
        required = await api.post('/api/auth/passkeys/policy', headers=owner_headers, json={'owners': 'allow', 'members': 'require'})
        assert required.status_code == 200, required.text
        body = {'username': 'alex', 'password': 'member password 123', 'code': code}
        member.cookies.clear()
        result = await member.post('/api/auth/passkeys/recover', headers={'Origin': ORIGIN}, json=body)
        assert result.status_code == 409
        assert (await api.get('/api/auth/passkeys')).json()['policy']['members'] == 'require'
        # An owner must explicitly permit fallback; the blocked recovery did not consume the code.
        assert (await api.post('/api/auth/passkeys/policy', headers=owner_headers,
                              json={'owners': 'allow', 'members': 'allow'})).status_code == 200
        recovered = await member.post('/api/auth/passkeys/recover', headers={'Origin': ORIGIN}, json=body)
        assert recovered.status_code == 200, recovered.text
