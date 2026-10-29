"""Native lifecycle refusals that coverage showed untested: rotation, consent changes while signing, and edge gates."""
from datetime import timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from helpers import verified_post, write_db
from sqlalchemy import select
from test_mobile import (  # noqa: F401 (mobile: a fixture)
    INSTALL,
    PREFIX,
    activate,
    begin,
    claim_body,
    issue,
    mobile,
    request_key,
)
from test_users import browser, join

from acc.server import auth, devices
from acc.server.models import AuditLog, AuthSession, Device, DevicePairing, Setting, User


async def claim(api, pending, key=None, install=INSTALL):
    result = await api.post(PREFIX + '/claim', json=claim_body(pending, key=key, install=install))
    assert result.status_code == 200, result.text
    return result.json()


async def confirm(api, headers, pending, claimed):
    return await api.post(PREFIX + '/pairings/' + pending['id'] + '/confirm', headers=headers,
                          json={'matching_code': claimed['matching_code']})


def pairing(api, id):
    with api.app.state.db() as s:
        return s.get(DevicePairing, id).status


def device_rows(api):
    with api.app.state.db() as s:
        return [(d.id, d.status, d.fingerprint) for d in s.scalars(select(Device))]


def refusals(api, operation):
    with api.app.state.db() as s:
        return [r for r in s.scalars(select(AuditLog.detail).where(AuditLog.action == 'mobile.refused'))
                if r['operation'] == operation]


def fresh_owner(api):
    """Revoke ends every session of the device's person (here the owner): sign in again, recently verified."""
    with write_db(api.app.state.db) as s:
        token, csrf = auth.create_session(s, s.scalar(select(User).where(User.username == 'casey')), 'Recovery')
        s.get(AuthSession, auth.digest(token)).reauthenticated_at = auth.now()
        s.commit()
    api.cookies.set('acc_session', token)
    return {'X-ACC-CSRF': csrf}


async def revoke(api, headers, id):
    return await api.post(PREFIX + '/devices/' + id + '/revoke', headers=headers, json={})


def interrupt_signing(monkeypatch, change):
    """Run `change` between the signer's answer and the second transaction, like a concurrent owner action."""
    sign = devices.sign
    def interrupted(body):
        result = sign(body)
        change()
        return result
    monkeypatch.setattr(devices, 'sign', interrupted)


async def test_rotation_refuses_unknown_another_persons_and_revoked_devices(api, mobile):  # noqa: F811
    credential, _, _, _ = await issue(api, mobile)
    body = {'label': 'Phone', 'person': 'casey'}
    assert (await api.post(PREFIX + '/devices/' + '0' * 32 + '/rotate', headers=mobile, json=body)).status_code == 404
    # The person in the request must be the device's person: no rotating a leaf into someone else's name.
    other = await api.post(PREFIX + '/devices/' + credential['device_id'] + '/rotate', headers=mobile,
                           json={**body, 'person': 'alex'})
    assert other.status_code == 404, other.text
    assert (await revoke(api, mobile, credential['device_id'])).status_code == 200
    revoked = await api.post(PREFIX + '/devices/' + credential['device_id'] + '/rotate', headers=fresh_owner(api),
                             json=body)
    assert revoked.status_code == 409, revoked.text
    with api.app.state.db() as s:
        assert s.scalar(select(DevicePairing).where(DevicePairing.renew_id.is_not(None))) is None
    assert [r['status'] for r in refusals(api, 'rotate')] == [404, 404, 409]


async def test_rotation_claim_needs_the_same_installation_and_a_fresh_key(api, mobile):  # noqa: F811
    key = request_key()
    pending = await begin(api, mobile)
    claimed = await claim(api, pending, key=key)
    assert (await confirm(api, mobile, pending, claimed)).status_code == 200
    device_id = device_rows(api)[0][0]
    rotation = await begin(api, mobile, renew=device_id)
    for kwargs in ({'install': 'b' * 32}, {'key': key}):  # another installation; the same key again
        result = await api.post(PREFIX + '/claim', json=claim_body(rotation, **kwargs))
        assert result.status_code == 409, (kwargs, result.text)
        assert pairing(api, rotation['id']) == 'pending'  # a refused claim never consumes the pairing
    assert (await confirm(api, mobile, rotation, await claim(api, rotation))).status_code == 200


async def test_device_revoked_while_its_rotation_signs_stays_revoked(api, mobile, monkeypatch):  # noqa: F811
    credential, _, _, _ = await issue(api, mobile)
    before = device_rows(api)
    rotation = await begin(api, mobile, renew=credential['device_id'])
    claimed = await claim(api, rotation)
    def revoke_old():
        with write_db(api.app.state.db) as s:
            s.get(Device, credential['device_id']).status = 'revoked'
            s.commit()
    interrupt_signing(monkeypatch, revoke_old)
    result = await confirm(api, mobile, rotation, claimed)
    assert result.status_code == 409 and 'revoked' in result.json()['detail'], result.text
    # Neither reactivated nor given the freshly signed leaf, and the pairing can't be retried.
    assert device_rows(api) == [(before[0][0], 'revoked', before[0][2])]
    assert pairing(api, rotation['id']) == 'cancelled'
    assert (await api.post(PREFIX + '/credential', json={'receipt': claimed['receipt']})).status_code == 410


async def test_device_replaced_while_its_rotation_signs_is_not_overwritten(api, mobile, monkeypatch):  # noqa: F811
    credential, _, _, _ = await issue(api, mobile)
    rotation = await begin(api, mobile, renew=credential['device_id'])
    claimed = await claim(api, rotation)
    def replace_leaf():
        with write_db(api.app.state.db) as s:
            s.get(Device, credential['device_id']).fingerprint = 'f' * 64
            s.commit()
    interrupt_signing(monkeypatch, replace_leaf)
    assert (await confirm(api, mobile, rotation, claimed)).status_code == 409
    assert device_rows(api)[0][2] == 'f' * 64
    assert pairing(api, rotation['id']) == 'cancelled'


async def add_member(api, name='alex'):
    invitation = await verified_post(api, '/api/users/invites', {'role': 'member', 'account': 'dana-agent'})
    assert invitation.status_code == 201, invitation.text
    async with browser(api) as member:
        assert (await join(member, invitation.json()['link'], name)).status_code == 200


async def test_target_person_removed_while_signing_gets_no_identity(api, mobile, monkeypatch, member_accounts):  # noqa: F811
    await add_member(api)
    pending = await begin(api, mobile, person='alex')
    claimed = await claim(api, pending)
    def remove_alex():
        with write_db(api.app.state.db) as s:
            user = s.scalar(select(User).where(User.username == 'alex'))
            for session in s.scalars(select(AuthSession).where(AuthSession.user_id == user.id)):
                s.delete(session)
            s.delete(user)
            s.commit()
    interrupt_signing(monkeypatch, remove_alex)
    result = await confirm(api, mobile, pending, claimed)
    assert result.status_code == 409 and 'person was removed' in result.json()['detail'], result.text
    assert device_rows(api) == []
    assert pairing(api, pending['id']) == 'cancelled'


async def test_owner_verification_lapsing_while_signing_cancels(api, mobile, monkeypatch):  # noqa: F811
    pending = await begin(api, mobile)
    claimed = await claim(api, pending)
    def lapse():
        with write_db(api.app.state.db) as s:
            s.get(AuthSession, auth.digest(api.cookies.get('acc_session'))).reauthenticated_at = \
                auth.now() - timedelta(minutes=6)
            s.commit()
    interrupt_signing(monkeypatch, lapse)
    assert (await confirm(api, mobile, pending, claimed)).status_code == 403
    assert device_rows(api) == []
    assert pairing(api, pending['id']) == 'cancelled'


async def test_confirm_refuses_before_claim_and_while_edge_policy_is_pending(api, mobile):  # noqa: F811
    pending = await begin(api, mobile)
    early = await api.post(PREFIX + '/pairings/' + pending['id'] + '/confirm', headers=mobile, json={'matching_code': '000000'})
    assert early.status_code == 409, early.text
    claimed = await claim(api, pending)
    with write_db(api.app.state.db) as s:
        s.merge(Setting(key='remote_access_runtime', value={'pending': True}))
        s.commit()
    held = await confirm(api, mobile, pending, claimed)
    assert held.status_code == 409 and 'public access change is still pending' in held.json()['detail'], held.text
    # Held, not cancelled: once the policy settles the same claim can be confirmed.
    assert pairing(api, pending['id']) == 'claimed' and device_rows(api) == []
    with write_db(api.app.state.db) as s:
        s.merge(Setting(key='remote_access_runtime', value={'pending': False}))
        s.commit()
    assert (await confirm(api, mobile, pending, claimed)).status_code == 200


async def test_confirm_retry_after_approval_needs_the_same_code(api, mobile):  # noqa: F811
    pending = await begin(api, mobile)
    claimed = await claim(api, pending)
    first = await confirm(api, mobile, pending, claimed)
    assert first.status_code == 200, first.text
    again = await confirm(api, mobile, pending, claimed)
    assert again.status_code == 200 and again.json()['device_id'] == first.json()['device_id']
    other = '000000' if claimed['matching_code'] != '000000' else '999999'
    wrong = await api.post(PREFIX + '/pairings/' + pending['id'] + '/confirm', headers=mobile, json={'matching_code': other})
    assert wrong.status_code == 409, wrong.text
    assert len(device_rows(api)) == 1 and pairing(api, pending['id']) == 'ready'


async def test_start_refuses_unknown_person_and_caps_open_pairings(api, mobile):  # noqa: F811
    unknown = await api.post(PREFIX + '/pairings', headers=mobile, json={'label': 'Phone', 'person': 'nobody'})
    assert unknown.status_code == 404, unknown.text
    for _ in range(10):
        await begin(api, mobile)
    capped = await api.post(PREFIX + '/pairings', headers=mobile, json={'label': 'Phone', 'person': 'casey'})
    assert capped.status_code == 429, capped.text
    with api.app.state.db() as s:
        assert len(list(s.scalars(select(DevicePairing)))) == 10


async def test_an_enrolled_installation_cannot_enroll_again_without_rotation(api, mobile):  # noqa: F811
    await issue(api, mobile)
    pending = await begin(api, mobile)
    result = await confirm(api, mobile, pending, await claim(api, pending))  # same installation ID, fresh key
    assert result.status_code == 409 and 'already paired' in result.json()['detail'], result.text
    assert len(device_rows(api)) == 1
    assert pairing(api, pending['id']) == 'cancelled'


async def test_revoke_unknown_device_is_refused_and_listing_marks_revoked_inactive(api, mobile):  # noqa: F811
    assert (await revoke(api, mobile, '0' * 32)).status_code == 404
    credential, _, _, _ = await issue(api, mobile)
    assert (await revoke(api, mobile, credential['device_id'])).status_code == 200
    fresh_owner(api)
    listed = (await api.get(PREFIX + '/devices')).json()['devices']
    assert [(d['id'], d['activation']) for d in listed] == [(credential['device_id'], 'inactive')]


async def test_claim_from_a_foreign_origin_is_refused(api, mobile):  # noqa: F811
    pending = await begin(api, mobile)
    result = await api.post(PREFIX + '/claim', headers={'Origin': 'https://attacker.example.com'}, json=claim_body(pending))
    assert result.status_code == 403 and result.json()['detail'] == 'bad origin', result.text
    assert pairing(api, pending['id']) == 'pending'


def secp256k1_request():
    """256 bits with a short enough proof, but not P-256: the curve check itself must refuse it."""
    key = ec.generate_private_key(ec.SECP256K1())
    csr = (x509.CertificateSigningRequestBuilder().subject_name(x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, 'phone.example.com')]))
        .sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM).decode())
    return key, csr


async def test_claim_refuses_a_non_p256_key(api, mobile):  # noqa: F811
    pending = await begin(api, mobile)
    result = await api.post(PREFIX + '/claim', json=claim_body(pending, key=secp256k1_request()))
    assert result.status_code == 422 and result.json()['detail'] == "The app's pairing request could not be verified. Start pairing again.", result.text
    assert pairing(api, pending['id']) == 'pending'


@pytest.mark.parametrize('path', ['/api/mobile/v1/devices/{id}/rotate', '/api/projects'])
async def test_edge_refuses_rotation_and_identities_of_removed_people(api, mobile, member_accounts, path):  # noqa: F811
    await add_member(api)
    credential, _, _, _ = await issue(api, mobile, await begin(api, mobile, person='alex'))
    await activate(api)
    ctx = api.app.state.ctx
    fingerprint = devices.fingerprint(x509.load_pem_x509_certificate(credential['certificate'].encode()))
    target = path.format(id=credential['device_id'])
    assert ctx.edge.check(fingerprint, '/api/projects') is not None  # admitted: the refusals below are real
    if 'rotate' in target:  # rotation is private-LAN only, even for a valid, current leaf
        assert ctx.edge.check(fingerprint, target) is None
        return
    with write_db(api.app.state.db) as s:
        user = s.scalar(select(User).where(User.username == 'alex'))
        for session in s.scalars(select(AuthSession).where(AuthSession.user_id == user.id)):
            s.delete(session)
        s.delete(user)
        s.commit()
    assert ctx.edge.check(fingerprint, target) is None  # the leaf is still "active", its person is gone


@pytest.mark.parametrize('assertion', [None, 'f' * 64])
async def test_native_websocket_without_the_trusted_edge_assertion_is_closed(api, mobile, assertion):  # noqa: F811
    import asyncio
    credential, _, _, _ = await issue(api, mobile)
    await activate(api)
    fingerprint = devices.fingerprint(x509.load_pem_x509_certificate(credential['certificate'].encode()))
    headers = [(b'host', b'remote.example.com'), (b'x-acc-edge-client', fingerprint.encode()),
               (b'origin', mobile['Origin'].encode())]
    if assertion:  # a forged assertion is as good as none
        headers.append((b'x-acc-native-edge', assertion.encode()))
    scope = {'type': 'websocket', 'asgi': {'version': '3.0'}, 'scheme': 'wss', 'path': '/api/events',
             'raw_path': b'/api/events', 'root_path': '', 'query_string': b'', 'server': ('remote.example.com', 443),
             'client': None, 'subprotocols': [], 'headers': headers}
    inbox, sent = asyncio.Queue(), []
    await inbox.put({'type': 'websocket.connect'})
    async def send(message):
        sent.append(message)
    await asyncio.wait_for(api.app(scope, inbox.get, send), 10)
    assert sent == [{'type': 'websocket.close', 'code': 4403}]  # closed before any accept or handler
