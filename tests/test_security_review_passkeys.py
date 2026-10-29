"""Passkey security review (docs/reviews/2026-10-23-security-review-passkeys.md): proofs and boundaries that held."""
import os
from datetime import timedelta

from helpers import TOTP_AT, write_db
from sqlalchemy import select
from test_auth import PASSWORD
from test_passkeys import ORIGIN, Authenticator, enroll, setup
from test_users import browser, join

from acc.server import auth
from acc.server.models import AttentionItem, AuditLog, AuthSession, PasskeyChallenge, PasskeyPolicy, User


def expire_reauthentication(api):
    with write_db(api.app.state.db) as s:
        s.get(AuthSession, auth.digest(api.cookies.get('acc_session'))).reauthenticated_at = None
        s.commit()


async def enrolled_member(api, owner_headers, keys=1):
    """alex, a member with `keys` passkeys and a recovery code; returns their user id."""
    link = (await api.post('/api/users/invites', headers=owner_headers,
                           json={'role': 'member', 'account': 'dana-agent'})).json()['link']
    async with browser(api) as member:
        await join(member, link, 'alex')
        headers = {'Origin': ORIGIN, 'X-ACC-CSRF': member.cookies.get('acc_csrf')}
        with api.app.state.db() as s:
            user = s.scalar(select(User).where(User.username == 'alex'))
            secret, uid = user.totp_secret, user.id
        verified = await member.post('/api/auth/passkeys/reauth', headers=headers, json={
            'password': 'member password 123', 'code': auth.totp(secret, TOTP_AT + 30)})
        assert verified.status_code == 200, verified.text
        for n in range(keys):
            await enroll(member, headers, Authenticator(), f'Member key {n}')
        assert (await member.post('/api/auth/passkeys/recovery-code', headers=headers, json={})).status_code == 200
    return uid


def require_owner_passkeys(api):
    with write_db(api.app.state.db) as s:
        s.merge(PasskeyPolicy(id=1, owners='require', members='allow'))
        s.commit()


# ---------------------------------------------------------------- PK-01

async def test_stolen_owner_session_cannot_promote_into_required_owners(api, env, monkeypatch, member_accounts):
    owner_headers, _ = await setup(api, env, monkeypatch)
    device = Authenticator()
    _, handle = await enroll(api, owner_headers, device)
    uid = await enrolled_member(api, owner_headers, keys=2)
    require_owner_passkeys(api)
    expire_reauthentication(api)  # a copied cookie: signed in, but no recent verification
    result = await api.post(f'/api/users/{uid}/role', headers=owner_headers, json={'role': 'owner'})
    assert result.status_code == 403, result.text
    # A real owner verifies with a passkey (required for owners now), then promotes.
    begin = (await api.post('/api/auth/passkeys/verify/begin', headers=owner_headers, json={})).json()
    verified = await api.post('/api/auth/passkeys/verify/complete', headers=owner_headers, json={
        'challenge_id': begin['challenge_id'], 'credential': device.credential(begin['options'], handle=handle)})
    assert verified.status_code == 200, verified.text
    assert (await api.post(f'/api/users/{uid}/role', headers=owner_headers, json={'role': 'owner'})).status_code == 200


async def test_password_reauthentication_does_not_satisfy_owner_promotion_under_require(api, env, monkeypatch, member_accounts):
    owner_headers, _ = await setup(api, env, monkeypatch)  # verified with password and authenticator
    await enroll(api, owner_headers, Authenticator())
    uid = await enrolled_member(api, owner_headers, keys=2)
    require_owner_passkeys(api)
    assert (await api.post(f'/api/users/{uid}/role', headers=owner_headers, json={'role': 'owner'})).status_code == 403


async def test_promotion_under_allow_needs_no_reauthentication(api, env, monkeypatch, member_accounts):
    owner_headers, _ = await setup(api, env, monkeypatch)
    uid = await enrolled_member(api, owner_headers)
    expire_reauthentication(api)
    assert (await api.post(f'/api/users/{uid}/role', headers=owner_headers, json={'role': 'owner'})).status_code == 200


# ---------------------------------------------------------------- PK-02

async def test_account_throttle_does_not_block_a_correct_password_reauthentication(api, env, monkeypatch):
    headers, secret = await setup(api, env, monkeypatch)
    for _ in range(10):  # someone else failed sign-in for this name from other addresses
        api.app.state.ctx.throttle.fail('user:casey')
    monkeypatch.setattr(auth.time, 'time', lambda: TOTP_AT + 60)
    wrong = await api.post('/api/auth/passkeys/reauth', headers=headers, json={'password': 'wrong', 'code': '000000'})
    assert wrong.status_code == 429
    right = await api.post('/api/auth/passkeys/reauth', headers=headers, json={
        'password': PASSWORD, 'code': auth.totp(secret, TOTP_AT + 60)})
    assert right.status_code == 200, right.text


# ---------------------------------------------------------------- PK-03

async def test_anonymous_challenge_flood_cannot_block_signin_or_verification(api, env, monkeypatch):
    headers, _ = await setup(api, env, monkeypatch)
    device = Authenticator()
    _, handle = await enroll(api, headers, device)
    with write_db(api.app.state.db) as s:  # unauthenticated begins from many addresses
        for n in range(1000):
            s.add(PasskeyChallenge(id=f'flood{n}', challenge='x', purpose='login', origin=ORIGIN,
                                   client=f'198.51.100.{n % 250}', expires_at=auth.now() + timedelta(minutes=5)))
        s.commit()
    verify = await api.post('/api/auth/passkeys/verify/begin', headers=headers, json={})
    assert verify.status_code == 200, verify.text
    api.cookies.clear()
    begin = await api.post('/api/auth/passkeys/login/begin', headers={'Origin': ORIGIN}, json={})
    assert begin.status_code == 200, begin.text
    done = await api.post('/api/auth/passkeys/login/complete', headers={'Origin': ORIGIN}, json={
        'challenge_id': begin.json()['challenge_id'], 'credential': device.credential(begin.json()['options'], handle=handle)})
    assert done.status_code == 200, done.text
    with api.app.state.db() as s:  # the oldest pending anonymous challenge made room
        assert len(list(s.scalars(select(PasskeyChallenge.id).where(PasskeyChallenge.id.like('flood%'))))) == 999


# ---------------------------------------------------------------- PK-04

async def test_signed_counter_regression_alerts_as_a_possible_clone(api, env, monkeypatch):
    headers, _ = await setup(api, env, monkeypatch)
    device = Authenticator()
    _, handle = await enroll(api, headers, device)  # stored counter 1
    real = device.key
    for key in (Authenticator().key, real):  # first a forgery (wrong key), then the real key, both with counter 1
        device.key, device.count = key, 0
        begin = (await api.post('/api/auth/passkeys/login/begin', headers=headers, json={})).json()
        result = await api.post('/api/auth/passkeys/login/complete', headers=headers, json={
            'challenge_id': begin['challenge_id'], 'credential': device.credential(begin['options'], handle=handle)})
        assert result.status_code == 401
        with api.app.state.db() as s:
            alerts = list(s.scalars(select(AttentionItem).where(AttentionItem.dedupe_key.like('passkey.clone.%'))))
        assert len(alerts) == (1 if key is real else 0)  # an unsigned guess can't raise false alarms
    assert alerts[0].owner == 'casey'
    with api.app.state.db() as s:
        details = [a.detail for a in s.scalars(select(AuditLog).where(AuditLog.action == 'auth.passkey_failed'))]
    assert {'reason': 'counter_regressed'} in details


# ---------------------------------------------------------------- PK-05

async def test_owner_recovery_and_policy_relaxation_alert_owners_with_details(api, env, monkeypatch):
    headers, _ = await setup(api, env, monkeypatch)
    code = (await api.post('/api/auth/passkeys/recovery-code', headers=headers, json={})).json()['code']
    for policy in ({'owners': 'allow', 'members': 'require'}, {'owners': 'allow', 'members': 'allow'}):
        assert (await api.post('/api/auth/passkeys/policy', headers=headers, json=policy)).status_code == 200
    with api.app.state.db() as s:
        changes = [a.detail for a in s.scalars(select(AuditLog).where(AuditLog.action == 'auth.passkey_policy_changed').order_by(AuditLog.id))]
        assert changes[-1] == {'from': {'owners': 'allow', 'members': 'require'}, 'to': {'owners': 'allow', 'members': 'allow'}}
        assert s.scalar(select(AttentionItem).where(AttentionItem.dedupe_key == 'passkey.policy_relaxed')) is not None
        assert 'auth.passkey_reauth' in set(s.scalars(select(AuditLog.action)))
    api.cookies.clear()
    recovered = await api.post('/api/auth/passkeys/recover', headers={'Origin': ORIGIN}, json={
        'username': 'casey', 'password': PASSWORD, 'code': code})
    assert recovered.status_code == 200, recovered.text
    with api.app.state.db() as s:
        item = s.scalar(select(AttentionItem).where(AttentionItem.dedupe_key == 'passkey.recovered.casey'))
    assert item is not None and item.severity == 'critical'


# ---------------------------------------------------------------- PK-06

async def test_registration_refuses_credential_ids_over_the_webauthn_limit(api, env, monkeypatch):
    headers, _ = await setup(api, env, monkeypatch)
    device = Authenticator()
    device.id = os.urandom(1024)
    begin = (await api.post('/api/auth/passkeys/register/begin', headers=headers, json={})).json()
    result = await api.post('/api/auth/passkeys/register/complete', headers=headers, json={
        'challenge_id': begin['challenge_id'], 'name': 'Long',
        'credential': device.credential(begin['options'], registration=True)})
    assert result.status_code == 401
    assert not (await api.get('/api/auth/passkeys')).json()['passkeys']


# ---------------------------------------------------------------- boundaries that held

async def test_challenges_cannot_cross_between_registration_and_signin(api, env, monkeypatch):
    headers, _ = await setup(api, env, monkeypatch)
    device = Authenticator()
    _, handle = await enroll(api, headers, device)
    register = (await api.post('/api/auth/passkeys/register/begin', headers=headers, json={})).json()
    result = await api.post('/api/auth/passkeys/login/complete', headers=headers, json={
        'challenge_id': register['challenge_id'], 'credential': device.credential(register['options'] | {'rpId': '127.0.0.1'}, handle=handle)})
    assert result.status_code == 401
    login = (await api.post('/api/auth/passkeys/login/begin', headers=headers, json={})).json()
    result = await api.post('/api/auth/passkeys/register/complete', headers=headers, json={
        'challenge_id': login['challenge_id'], 'name': 'Swap',
        'credential': Authenticator().credential(login['options'] | {'rp': {'id': '127.0.0.1'}}, registration=True)})
    assert result.status_code == 401
    assert len((await api.get('/api/auth/passkeys')).json()['passkeys']) == 1


async def test_member_cannot_register_an_owner_credential_id(api, env, monkeypatch, member_accounts):
    owner_headers, _ = await setup(api, env, monkeypatch)
    device = Authenticator()
    _, owner_handle = await enroll(api, owner_headers, device)
    link = (await api.post('/api/users/invites', headers=owner_headers,
                           json={'role': 'member', 'account': 'dana-agent'})).json()['link']
    async with browser(api) as member:
        await join(member, link, 'alex')
        headers = {'Origin': ORIGIN, 'X-ACC-CSRF': member.cookies.get('acc_csrf')}
        with api.app.state.db() as s:
            secret = s.scalar(select(User).where(User.username == 'alex')).totp_secret
        await member.post('/api/auth/passkeys/reauth', headers=headers, json={
            'password': 'member password 123', 'code': auth.totp(secret, TOTP_AT + 30)})
        begin = (await member.post('/api/auth/passkeys/register/begin', headers=headers, json={})).json()
        clash = Authenticator()
        clash.id = device.id  # the same credential ID, the member's own key pair
        result = await member.post('/api/auth/passkeys/register/complete', headers=headers, json={
            'challenge_id': begin['challenge_id'], 'name': 'Clash',
            'credential': clash.credential(begin['options'], registration=True)})
        assert result.status_code == 401
    # The owner's credential still signs in as the owner only.
    api.cookies.clear()
    begin = (await api.post('/api/auth/passkeys/login/begin', headers={'Origin': ORIGIN}, json={})).json()
    done = await api.post('/api/auth/passkeys/login/complete', headers={'Origin': ORIGIN}, json={
        'challenge_id': begin['challenge_id'], 'credential': device.credential(begin['options'], handle=owner_handle)})
    assert done.status_code == 200
    assert (await api.get('/api/auth/state')).json()['username'] == 'casey'


async def test_spoofed_forwarded_for_does_not_buy_more_signin_attempts_over_tcp(api, env):
    for n in range(5):
        assert (await api.post('/api/auth/passkeys/login/begin', headers={
            'Origin': ORIGIN, 'X-Forwarded-For': f'203.0.113.{n}'}, json={})).status_code == 200
    assert (await api.post('/api/auth/passkeys/login/begin', headers={
        'Origin': ORIGIN, 'X-Forwarded-For': '203.0.113.99'}, json={})).status_code == 429


async def test_reauth_challenge_belongs_to_one_login_session(api, env, monkeypatch):
    headers, _ = await setup(api, env, monkeypatch)
    device = Authenticator()
    _, handle = await enroll(api, headers, device)
    async with browser(api) as other:  # the same person, signed in on another device
        begin = (await other.post('/api/auth/passkeys/login/begin', headers={'Origin': ORIGIN}, json={})).json()
        assert (await other.post('/api/auth/passkeys/login/complete', headers={'Origin': ORIGIN}, json={
            'challenge_id': begin['challenge_id'], 'credential': device.credential(begin['options'], handle=handle)})).status_code == 200
        begin = (await api.post('/api/auth/passkeys/verify/begin', headers=headers, json={})).json()
        other.cookies.set('acc_webauthn', begin['challenge_id'])  # even with this browser's ceremony cookie copied
        result = await other.post('/api/auth/passkeys/verify/complete', headers={
            'Origin': ORIGIN, 'X-ACC-CSRF': other.cookies.get('acc_csrf')}, json={
            'challenge_id': begin['challenge_id'], 'credential': device.credential(begin['options'], handle=handle)})
        assert result.status_code == 401


# ---------------------------------------------------------------- PK-07 (owner decision: whatever the policy)

def refusals(api, action):
    with api.app.state.db() as s:
        return [(a.actor, a.target, a.detail) for a in s.scalars(select(AuditLog).where(AuditLog.action == action))]


async def test_inviting_an_owner_needs_fresh_verification_under_allow(api, env, monkeypatch, member_accounts):
    headers, secret = await setup(api, env, monkeypatch)
    expire_reauthentication(api)  # a copied cookie, passkeys allowed (not required)
    refused = await api.post('/api/users/invites', headers=headers, json={'role': 'owner'})
    assert refused.status_code == 403, refused.text
    assert refusals(api, 'user.invite_refused') == [('casey', 'owner', {'reason': 'verification_required'})]
    member = await api.post('/api/users/invites', headers=headers, json={'role': 'member', 'account': 'dana-agent'})
    assert member.status_code == 201  # member invites are unchanged
    monkeypatch.setattr(auth.time, 'time', lambda: TOTP_AT + 60)
    assert (await api.post('/api/auth/passkeys/reauth', headers=headers, json={
        'password': PASSWORD, 'code': auth.totp(secret, TOTP_AT + 60)})).status_code == 200
    assert (await api.post('/api/users/invites', headers=headers, json={'role': 'owner'})).status_code == 201


async def test_resetting_another_account_needs_fresh_verification_under_allow(api, env, monkeypatch, member_accounts):
    headers, secret = await setup(api, env, monkeypatch)
    uid = await enrolled_member(api, headers)
    expire_reauthentication(api)
    refused = await api.post(f'/api/users/{uid}/reset', headers=headers, json={})
    assert refused.status_code == 403, refused.text
    assert refusals(api, 'user.reset_refused') == [('casey', 'alex', {'reason': 'verification_required'})]
    with api.app.state.db() as s:  # no link was made
        assert s.scalar(select(AuditLog).where(AuditLog.action == 'user.reset_link')) is None
    # Your own reset stays available: it is the sole owner's way back after recovery with a lost authenticator.
    me = (await api.get('/api/users')).json()['users'][0]['id']
    assert (await api.post(f'/api/users/{me}/reset', headers=headers, json={})).status_code == 201
    monkeypatch.setattr(auth.time, 'time', lambda: TOTP_AT + 60)
    assert (await api.post('/api/auth/passkeys/reauth', headers=headers, json={
        'password': PASSWORD, 'code': auth.totp(secret, TOTP_AT + 60)})).status_code == 200
    assert (await api.post(f'/api/users/{uid}/reset', headers=headers, json={})).status_code == 201


async def test_api_token_cannot_invite_owners_or_reset_people(api, env, monkeypatch, member_accounts):
    from test_auth import do_setup
    await do_setup(api, env)  # api still carries the bearer token, no human session
    assert (await api.post('/api/users/invites', headers={'Origin': ORIGIN}, json={'role': 'owner'})).status_code == 403
    uid = (await api.get('/api/users')).json()['users'][0]['id']
    assert (await api.post(f'/api/users/{uid}/reset', headers={'Origin': ORIGIN}, json={})).status_code == 403
    assert [r[0] for r in refusals(api, 'user.invite_refused') + refusals(api, 'user.reset_refused')] == ['@api', '@api']
