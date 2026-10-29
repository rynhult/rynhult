"""Native enrollment must preserve private pairing, person auth and the edge gate."""
import base64
import secrets
from dataclasses import replace
from datetime import timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from helpers import write_db
from sqlalchemy import delete, select
from test_device_ca import helper
from test_devices import enable
from test_passkeys import setup

from acc.server import auth, devices
from acc.server.models import AuditLog, AuthSession, Device, DevicePairing, Setting

PREFIX = '/api/mobile/v1'
INSTALL = 'a' * 32


def request_key():
    key = ec.generate_private_key(ec.SECP256R1())
    csr = (x509.CertificateSigningRequestBuilder().subject_name(x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, 'attacker.example.com')]))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName('attacker.example.com')]), critical=False)
        .sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM).decode())
    return key, csr


def claim_body(pending, key=None, install=INSTALL):
    key, csr = request_key() if key is None else key
    token = pending['link'].split('#')[1].split(':')[1]
    # Independently encode the documented protocol, not the production encoder.
    message = '\n'.join(['acc-mobile-claim-v1', pending['id'], token,
                         'https://acc.example.lan', install, 'ios', auth.digest(csr)]).encode()
    signature = key.sign(message, ec.ECDSA(hashes.SHA256()))
    return {'token': token, 'installation_id': install, 'platform': 'ios', 'csr': csr,
            'proof': base64.urlsafe_b64encode(signature).decode().rstrip('=')}


@pytest.fixture
async def mobile(api, env, monkeypatch, tmp_path):
    headers, _ = await setup(api, env, monkeypatch)
    await enable(api, headers)
    ctx = api.app.state.ctx
    monkeypatch.setattr(ctx, 'settings', replace(ctx.settings, mobile_enabled=True))
    ca = helper()
    ca.initialize(tmp_path / 'authority')
    (ctx.settings.data_dir / 'device-ca.pem').write_bytes((tmp_path / 'authority/ca.pem').read_bytes())
    monkeypatch.setattr(devices, 'sign', lambda body: ca.issue(tmp_path / 'authority', body))
    key = secrets.token_hex(32)
    secret_file = ctx.settings.data_dir / 'secrets/mobile-edge-key'
    secret_file.write_text(key)
    secret_file.chmod(0o600)
    api.headers['X-Acc-Native-Edge'] = key
    api.headers['Host'] = 'acc.example.lan'
    return headers


async def begin(api, headers, person='casey', renew=None):
    body = {'label': 'Alex phone', 'person': person}
    path = PREFIX + '/pairings' if renew is None else PREFIX + '/devices/' + renew + '/rotate'
    result = await api.post(path, headers=headers, json=body)
    assert result.status_code == 200, result.text
    return result.json()


async def issue(api, headers, pending=None, install=INSTALL):
    pending = pending or await begin(api, headers)
    body = claim_body(pending, install=install)
    claimed = await api.post(PREFIX + '/claim', json=body)
    assert claimed.status_code == 200, claimed.text
    receipt = claimed.json()['receipt']
    confirmed = await api.post(PREFIX + '/pairings/' + pending['id'] + '/confirm', headers=headers,
                               json={'matching_code': claimed.json()['matching_code']})
    assert confirmed.status_code == 200, confirmed.text
    result = await api.post(PREFIX + '/credential', json={'receipt': receipt})
    assert result.status_code == 200, result.text
    return result.json(), pending, body, receipt


async def activate(api):
    from acc.remote_access_helper import revision
    payload = api.app.state.ctx.remote_access.payload()
    with write_db(api.app.state.db) as s:
        s.merge(Setting(key='remote_access_runtime', value={'enabled': True, 'desired_enabled': True,
            'pending': False, 'last_reload_result': 'verified',
            'registry_revision': revision(payload)}))
        s.commit()


async def test_mobile_is_off_by_default(api, env, monkeypatch):
    headers, _ = await setup(api, env, monkeypatch)
    result = await api.post(PREFIX + '/pairings', headers=headers, json={'label': 'Phone', 'person': 'casey'})
    assert result.status_code == 404, result.text


async def test_claim_issue_bind_and_owner_revoke(api, mobile):
    headers = mobile
    credential, pending, body, receipt = await issue(api, headers)
    cert = x509.load_pem_x509_certificate(credential['certificate'].encode())
    assert cert.public_key().public_numbers() == x509.load_pem_x509_csr(body['csr'].encode()).public_key().public_numbers()
    assert credential['activation'] == 'pending'
    assert set(credential) == {'device_id', 'certificate', 'chain', 'activation', 'expires_at'}
    assert credential['chain']
    # Simulate only the fixture's trusted Unix proxy boundary; never use a live edge.
    api._transport.client = None
    edge_headers = {**headers, 'Host': 'remote.example.com', 'X-Acc-Edge-Client': devices.fingerprint(cert)}
    assert (await api.get('/api/projects', headers=edge_headers)).status_code == 403  # public access is still off
    assert (await api.post(PREFIX + '/session/bind', headers=edge_headers, json={})).status_code == 403
    await activate(api)
    assert (await api.get('/api/projects', headers=edge_headers)).status_code == 401  # now admitted, not bound yet
    bound = await api.post(PREFIX + '/session/bind', headers=edge_headers, json={})
    assert bound.status_code == 200, bound.text
    assert bound.headers['cache-control'] == 'no-store'
    assert (await api.get('/api/projects', headers=edge_headers)).status_code == 200
    assert (await api.get('/api/projects')).status_code == 401  # stolen cookie alone
    # A bound cookie cannot be reused for another leaf, even one owned by this person.
    with api.app.state.db() as s:
        record = s.get(AuthSession, auth.digest(api.cookies.get('acc_session')))
        assert record.device_fingerprint == devices.fingerprint(cert)
        assert auth.now() + timedelta(hours=11) < record.expires_at <= auth.now() + timedelta(hours=12)
        assert record.expires_at <= cert.not_valid_after_utc
        assert s.get(Device, credential['device_id']).kind == 'native'
        pair = s.get(DevicePairing, pending['id'])
        assert pair.token_hash != body['token'] and pair.receipt_hash != receipt
        assert not any(secret in str(list(s.scalars(select(AuditLog.detail))))
                       for secret in (body['token'], receipt, body['proof']))
    # Lost-phone recovery is performed from a separate private owner sign-in.
    import httpx

    from acc.server.models import User
    with write_db(api.app.state.db) as s:
        token, csrf = auth.create_session(s, s.scalar(select(User).where(User.username == 'casey')), 'Recovery')
        s.get(AuthSession, auth.digest(token)).reauthenticated_at = auth.now()
        s.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://acc',
            cookies={'acc_session': token}, headers={'Host': 'acc.example.lan'}) as recovery:
        revoked = await recovery.post(PREFIX + '/devices/' + credential['device_id'] + '/revoke',
                                      headers={'X-ACC-CSRF': csrf}, json={})
    assert revoked.status_code == 200, revoked.text
    assert (await api.get('/api/projects', headers=edge_headers)).status_code == 403
    with api.app.state.db() as s:
        assert s.get(Device, credential['device_id']).status == 'revoked'
        assert list(s.scalars(select(AuthSession))) == []
        assert {'mobile.pairing_start', 'mobile.claim', 'mobile.issue', 'mobile.session_bind', 'mobile.revoke'} <= set(s.scalars(select(AuditLog.action)))


async def test_claim_secret_is_single_use_and_cannot_cross_browser_api(api, mobile):
    pending = await begin(api, mobile)
    body = claim_body(pending)
    assert (await api.post('/api/device-pairing/claim', json={'token': body['token']})).status_code == 410
    one = await api.post(PREFIX + '/claim', json=body)
    assert one.status_code == 200, one.text
    assert (await api.post(PREFIX + '/claim', json=body)).status_code == 410
    assert (await api.post('/api/device-pairing/status', json={'receipt': one.json()['receipt']})).status_code == 410


@pytest.mark.parametrize('change', [{'proof': 'x' * 80}, {'installation_id': 'b' * 32}, {'platform': 'android'}])
async def test_claim_proof_binds_key_installation_platform_and_pairing(api, mobile, change):
    pending = await begin(api, mobile)
    body = claim_body(pending)
    result = await api.post(PREFIX + '/claim', json={**body, **change})
    assert result.status_code == 422, result.text
    assert (await api.post(PREFIX + '/claim', json=body)).status_code == 200


async def test_wrong_owner_code_cancels_without_issuing(api, mobile):
    pending = await begin(api, mobile)
    claimed = (await api.post(PREFIX + '/claim', json=claim_body(pending))).json()
    bad = '000000' if claimed['matching_code'] != '000000' else '999999'
    result = await api.post(PREFIX + '/pairings/' + pending['id'] + '/confirm', headers=mobile, json={'matching_code': bad})
    assert result.status_code == 409, result.text
    assert (await api.post(PREFIX + '/credential', json={'receipt': claimed['receipt']})).status_code == 410
    with api.app.state.db() as s:
        assert list(s.scalars(select(Device))) == []


async def test_private_recent_human_only_and_rate_limit(api, mobile):
    for host in ('remote.example.com', 'REMOTE.EXAMPLE.COM.'):
        result = await api.post(PREFIX + '/claim', headers={'Host': host, 'X-Forwarded-For': '10.0.0.1'}, json=claim_body(await begin(api, mobile)))
        assert result.status_code == 403, result.text
    assert (await api.post(PREFIX + '/pairings', headers={'Authorization': 'Bearer ' + api.app.state.ctx.token},
                           json={'label': 'Phone', 'person': 'casey'})).status_code == 403
    for _ in range(10):
        await api.post(PREFIX + '/credential', json={'receipt': 'x' * 43})
    assert (await api.post(PREFIX + '/credential', json={'receipt': 'x' * 43})).status_code == 429


async def test_native_signin_cookie_is_device_bound_before_explicit_binding(api, mobile, monkeypatch):
    from helpers import TOTP_AT
    from test_auth import PASSWORD
    credential, _, _, _ = await issue(api, mobile)
    await activate(api)
    api._transport.client = None
    api.headers.update({'Host': 'remote.example.com', 'X-Acc-Edge-Client': auth.digest('unused')})
    cert = x509.load_pem_x509_certificate(credential['certificate'].encode())
    api.headers['X-Acc-Edge-Client'] = devices.fingerprint(cert)
    with api.app.state.db() as s:
        from acc.server.models import User
        secret = s.scalar(select(User.totp_secret).where(User.username == 'casey'))
    monkeypatch.setattr(auth.time, 'time', lambda: TOTP_AT + 60)
    signed = await api.post('/api/auth/login', headers={'Origin': mobile['Origin']},
                           json={'username': 'casey', 'password': PASSWORD, 'code': auth.totp(secret, TOTP_AT + 60)})
    assert signed.status_code == 200, signed.text
    cookie = api.cookies.get('acc_session')
    fingerprint = api.headers.pop('X-Acc-Edge-Client')
    assert (await api.get('/api/projects')).status_code == 401  # even before binding, no portable cookie
    api.headers['X-Acc-Edge-Client'] = fingerprint
    assert (await api.get('/api/projects')).status_code == 401  # explicit binding still required
    headers = {**mobile, 'X-ACC-CSRF': api.cookies.get('acc_csrf')}
    assert (await api.post(PREFIX + '/session/bind', headers=headers, json={})).status_code == 200
    assert (await api.get('/api/projects')).status_code == 200
    api.headers.pop('X-Acc-Edge-Client')
    assert (await api.get('/api/projects')).status_code == 401
    with api.app.state.db() as s:
        assert s.get(AuthSession, auth.digest(cookie)) is None  # binding rotates the login secret


async def test_concurrent_claims_and_confirmations_issue_one_leaf(api, mobile, monkeypatch, backend_url):
    import asyncio

    from helpers import pause_supervisor
    from race_helpers import guard_database, transaction_rendezvous
    await pause_supervisor(api)
    pending = await begin(api, mobile)
    body = claim_body(pending)
    with guard_database(api, monkeypatch) as db:
        with transaction_rendezvous(db, 'UPDATE device_pairing'):
            claims = await asyncio.gather(*[api.post(PREFIX + '/claim', json=body) for _ in range(2)])
        assert sorted(r.status_code for r in claims) == [200, 410], [r.text for r in claims]
        code = next(r.json()['matching_code'] for r in claims if r.status_code == 200)
        with transaction_rendezvous(db, 'UPDATE device_pairing'):
            approvals = await asyncio.gather(*[api.post(PREFIX + '/pairings/' + pending['id'] + '/confirm',
                headers=mobile, json={'matching_code': code}) for _ in range(2)])
        assert all(r.status_code in (200, 409) for r in approvals) and any(r.status_code == 200 for r in approvals)
    with api.app.state.db() as s:
        assert len(list(s.scalars(select(Device)))) == 1
        assert len(list(s.scalars(select(AuditLog).where(AuditLog.action == 'mobile.issue')))) == 1
    # Replayed approval is idempotent and never asks the root helper for another leaf.
    assert (await api.post(PREFIX + '/pairings/' + pending['id'] + '/confirm', headers=mobile,
                           json={'matching_code': code})).status_code == 200


async def test_rotation_has_zero_overlap_and_cannot_replay_old_receipt(api, mobile):
    from acc.server import edge
    old, _, _, receipt = await issue(api, mobile)
    await activate(api)  # rejection must be by credential, not merely public access being off
    ctx = api.app.state.ctx
    old_fingerprint = devices.fingerprint(x509.load_pem_x509_certificate(old['certificate'].encode()))
    assert ctx.edge.check(old_fingerprint, '/api/projects')
    connection = edge.Connection(devices.fingerprint(x509.load_pem_x509_certificate(old['certificate'].encode())))
    api.app.state.ctx.edge.open.add(connection)
    try:
        new, _, _, _ = await issue(api, mobile, await begin(api, mobile, renew=old['device_id']))
        assert new['device_id'] == old['device_id'] and new['certificate'] != old['certificate']
        assert ctx.edge.check(old_fingerprint, '/api/projects') is None
        assert ctx.edge.check(devices.fingerprint(x509.load_pem_x509_certificate(new['certificate'].encode())),
                              '/api/projects')
        assert (await api.post(PREFIX + '/credential', json={'receipt': receipt})).status_code == 409
        assert await api.app.state.ctx.edge.sweep() == 1
        assert connection.closed.is_set()
        assert old['certificate'] not in api.app.state.ctx.remote_access.payload()['leaves']
        assert new['certificate'] in api.app.state.ctx.remote_access.payload()['leaves']
    finally:
        api.app.state.ctx.edge.open.discard(connection)


@pytest.mark.parametrize('change', ['revoke', 'person-remove', 'owner-demote', 'pairing-off', 'feature-off', 'policy-failure'])
async def test_changes_while_signing_never_activate_a_leaf(api, mobile, monkeypatch, change):
    from acc.server.models import User
    pending = await begin(api, mobile)
    claimed = (await api.post(PREFIX + '/claim', json=claim_body(pending))).json()
    sign = devices.sign
    def interrupted(body):
        result = sign(body)
        with write_db(api.app.state.db) as s:
            if change == 'revoke':
                s.get(DevicePairing, pending['id']).status = 'cancelled'
            elif change == 'person-remove':
                s.execute(delete(AuthSession))
                s.execute(delete(User))
            elif change == 'owner-demote':
                s.scalar(select(User)).role = 'member'
            elif change == 'pairing-off':
                config = dict(s.get(Setting, 'remote_access').value)
                config['pairing_enabled'] = False
                s.get(Setting, 'remote_access').value = config
            elif change == 'policy-failure':
                s.merge(Setting(key='remote_access_runtime', value={'pending': True}))
            s.commit()
        if change == 'feature-off':
            monkeypatch.setattr(api.app.state.ctx, 'settings', replace(api.app.state.ctx.settings, mobile_enabled=False))
        return result
    monkeypatch.setattr(devices, 'sign', interrupted)
    result = await api.post(PREFIX + '/pairings/' + pending['id'] + '/confirm', headers=mobile,
                            json={'matching_code': claimed['matching_code']})
    assert result.status_code in (403, 409, 410), result.text
    with api.app.state.db() as s:
        assert list(s.scalars(select(Device))) == []


@pytest.mark.parametrize('change', ['expiry', 'feature-off'])
async def test_expiry_and_feature_off_remove_native_policy_and_close_streams(api, mobile, monkeypatch, change):
    from acc.server import edge
    credential, _, _, _ = await issue(api, mobile)
    await activate(api)
    fingerprint = devices.fingerprint(x509.load_pem_x509_certificate(credential['certificate'].encode()))
    connection = edge.Connection(fingerprint)
    api.app.state.ctx.edge.open.add(connection)
    try:
        if change == 'expiry':
            with write_db(api.app.state.db) as s:
                s.get(Device, credential['device_id']).expires_at = auth.now() - timedelta(seconds=1)
                s.commit()
        else:
            monkeypatch.setattr(api.app.state.ctx, 'settings', replace(api.app.state.ctx.settings, mobile_enabled=False))
        assert (await api.get('/api/projects', headers={'X-Acc-Edge-Client': fingerprint})).status_code == 403
        assert await api.app.state.ctx.edge.sweep() == 1
        assert connection.closed.is_set()
        assert credential['certificate'] not in api.app.state.ctx.remote_access.payload()['leaves']
        assert (await api.get('/api/projects')).status_code == 200  # ordinary private sign-in survives
    finally:
        api.app.state.ctx.edge.open.discard(connection)


async def test_untrusted_fingerprint_and_api_token_cannot_bind_or_bypass_person(api, mobile):
    credential, _, _, _ = await issue(api, mobile)
    await activate(api)
    fingerprint = devices.fingerprint(x509.load_pem_x509_certificate(credential['certificate'].encode()))
    forged = {**mobile, 'Host': 'remote.example.com', 'X-Acc-Edge-Client': fingerprint, 'X-Forwarded-For': '10.0.0.1'}
    assert (await api.post(PREFIX + '/session/bind', headers=forged, json={})).status_code == 403
    api._transport.client = None
    api.headers['Host'] = 'remote.example.com'
    assert (await api.get('/api/projects', headers={**forged, 'Authorization': 'Bearer ' + api.app.state.ctx.token})).status_code == 403


async def test_member_and_agent_cannot_manage_native_identities(api, mobile, member_accounts):
    from helpers import verified_post
    from test_users import browser, csrf, join
    invitation = await verified_post(api, '/api/users/invites', {'role': 'member', 'account': 'dana-agent'})
    assert invitation.status_code == 201, invitation.text
    async with browser(api) as member:
        assert (await join(member, invitation.json()['link'], 'alex')).status_code == 200
        member.headers['Host'] = 'acc.example.lan'
        for suffix, body in [('/pairings', {'person': 'alex', 'label': 'Phone'}),
                             ('/pairings/' + '0' * 32 + '/confirm', {'matching_code': '000000'}),
                             ('/devices/' + '0' * 32 + '/revoke', {}),
                             ('/devices/' + '0' * 32 + '/rotate', {'person': 'alex', 'label': 'Phone'})]:
            result = await member.post(PREFIX + suffix, headers=csrf(member), json=body)
            assert result.status_code == 403, (suffix, result.text)
        assert (await member.get(PREFIX + '/devices')).json() == {'devices': []}


async def test_pairing_expires_and_recent_verification_is_required(api, mobile):
    pending = await begin(api, mobile)
    with write_db(api.app.state.db) as s:
        s.get(DevicePairing, pending['id']).expires_at = auth.now() - timedelta(seconds=1)
        s.get(AuthSession, auth.digest(api.cookies.get('acc_session'))).reauthenticated_at = auth.now() - timedelta(minutes=6)
        s.commit()
    assert (await api.post(PREFIX + '/claim', json=claim_body(pending))).status_code == 410
    assert (await api.post(PREFIX + '/pairings', headers=mobile, json={'person': 'casey', 'label': 'Phone'})).status_code == 403


@pytest.mark.parametrize('bad', [None, [], {'certificate': 7}, OSError('test signer down')])
async def test_uncertain_signer_failure_cancels_instead_of_blind_retry(api, mobile, monkeypatch, bad):
    pending = await begin(api, mobile)
    claimed = (await api.post(PREFIX + '/claim', json=claim_body(pending))).json()
    def broken(body):
        if isinstance(bad, Exception):
            raise bad
        return bad
    monkeypatch.setattr(devices, 'sign', broken)
    path = PREFIX + '/pairings/' + pending['id'] + '/confirm'
    body = {'matching_code': claimed['matching_code']}
    assert (await api.post(path, headers=mobile, json=body)).status_code == 503
    assert (await api.post(path, headers=mobile, json=body)).status_code == 410
    with api.app.state.db() as s:
        assert list(s.scalars(select(Device))) == []
        assert s.get(DevicePairing, pending['id']).status == 'cancelled'
        assert s.scalar(select(AuditLog).where(AuditLog.action == 'mobile.issue_failed')) is not None


async def test_public_chain_cannot_silently_change_between_issue_and_delivery(api, mobile, tmp_path):
    credential, _, _, receipt = await issue(api, mobile)
    ca = helper()
    ca.initialize(tmp_path / 'replacement-ca')
    (api.app.state.ctx.settings.data_dir / 'device-ca.pem').write_bytes((tmp_path / 'replacement-ca/ca.pem').read_bytes())
    result = await api.post(PREFIX + '/credential', json={'receipt': receipt})
    assert result.status_code == 409, result.text
    assert credential['certificate'] not in result.text


async def test_native_revoke_closes_an_actual_terminal(api, project, mobile):
    import asyncio
    import json

    from helpers import wait_for

    from acc.server.models import User
    session = await api.post('/api/sessions', headers=mobile,
                             json={'project_id': project['id'], 'provider': 'claude', 'name': 'Phone terminal'})
    assert session.status_code == 201, session.text
    sid = session.json()['id']
    async def idle():
        return (await api.get('/api/sessions/' + sid)).json()['observed_state'] == 'idle'
    await wait_for(idle)
    credential, _, _, _ = await issue(api, mobile)
    await activate(api)
    fingerprint = devices.fingerprint(x509.load_pem_x509_certificate(credential['certificate'].encode()))
    # Keep an independent private owner sign-in for the lost-phone action.
    with write_db(api.app.state.db) as s:
        recovery_token, recovery_csrf = auth.create_session(s, s.scalar(select(User).where(User.username == 'casey')), 'Recovery')
        s.get(AuthSession, auth.digest(recovery_token)).reauthenticated_at = auth.now()
        s.commit()
    api._transport.client = None
    native_headers = {**mobile, 'Host': 'remote.example.com', 'X-Acc-Edge-Client': fingerprint}
    assert (await api.post(PREFIX + '/session/bind', headers=native_headers, json={})).status_code == 200
    inbox, sent, mode = asyncio.Queue(), [], asyncio.Event()
    async def send(message):
        sent.append(message)
        if message['type'] == 'websocket.send' and json.loads(message.get('text', '{}')).get('t') == 'mode':
            mode.set()
    scope = {'type': 'websocket', 'asgi': {'version': '3.0'}, 'scheme': 'wss', 'path': '/api/sessions/' + sid + '/tty',
             'raw_path': ('/api/sessions/' + sid + '/tty').encode(), 'root_path': '', 'query_string': b'',
             'server': ('remote.example.com', 443), 'client': None, 'subprotocols': [],
             'headers': [(b'host', b'remote.example.com'), (b'x-acc-edge-client', fingerprint.encode()),
                         (b'x-acc-native-edge', api.headers['X-Acc-Native-Edge'].encode()),
                         (b'origin', mobile['Origin'].encode()), (b'cookie', ('acc_session=' + api.cookies.get('acc_session')).encode())]}
    task = asyncio.create_task(api.app(scope, inbox.get, send))
    try:
        await inbox.put({'type': 'websocket.connect'})
        await asyncio.wait_for(mode.wait(), 10)  # real broker/PTY bridge has attached
        import httpx
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://acc',
                cookies={'acc_session': recovery_token}, headers={'Host': 'acc.example.lan'}) as recovery:
            result = await recovery.post(PREFIX + '/devices/' + credential['device_id'] + '/revoke',
                                          headers={'X-ACC-CSRF': recovery_csrf}, json={})
            assert result.status_code == 200, result.text
        await asyncio.wait_for(task, 10)
        assert any(m['type'] == 'websocket.close' and m['code'] in (4401, 4403) for m in sent)
        assert api.app.state.ctx.terminals == {} and api.app.state.ctx.edge.open == set()
    finally:
        await inbox.put({'type': 'websocket.disconnect', 'code': 1000})
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_native_identity_cannot_enter_unqualified_company_signin(api, mobile):
    credential, _, _, _ = await issue(api, mobile)
    api._transport.client = None
    headers = {'Host': 'remote.example.com', 'X-Acc-Edge-Client': devices.fingerprint(
        x509.load_pem_x509_certificate(credential['certificate'].encode()))}
    for path in ('/api/auth/oidc/start', '/api/auth/oidc/callback'):
        result = await api.get(path, headers=headers)
        assert result.status_code == 403, result.text


@pytest.mark.parametrize('value,enabled', [(None,False),('0',False),('false',False),('1',True),('true',True),('yes',True)])
def test_mobile_deployment_flag_requires_explicit_opt_in(monkeypatch, value, enabled):
    from acc.server import config
    monkeypatch.setattr(config, 'environment', {} if value is None else {'ACC_MOBILE': value})
    assert config.Settings.from_env().mobile_enabled is enabled


async def test_cookie_cannot_move_to_another_leaf_of_the_same_person(api, mobile):
    first, _, _, _ = await issue(api, mobile)
    second, _, _, _ = await issue(api, mobile, install='b' * 32)
    await activate(api)
    api._transport.client = None
    first_headers = {**mobile, 'Host': 'remote.example.com', 'X-Acc-Edge-Client': devices.fingerprint(
        x509.load_pem_x509_certificate(first['certificate'].encode()))}
    assert (await api.post(PREFIX + '/session/bind', headers=first_headers, json={})).status_code == 200
    assert (await api.get('/api/projects', headers=first_headers)).status_code == 200
    other = {**first_headers, 'X-Acc-Edge-Client': devices.fingerprint(
        x509.load_pem_x509_certificate(second['certificate'].encode()))}
    assert (await api.get('/api/projects', headers=other)).status_code == 401


async def test_claim_signature_cannot_be_moved_to_a_second_pairing_or_key(api, mobile):
    first, second = await begin(api, mobile), await begin(api, mobile)
    body = claim_body(first)
    substituted = {**body, 'csr': request_key()[1]}
    assert (await api.post(PREFIX + '/claim', json=substituted)).status_code == 422
    other = claim_body(second)
    assert (await api.post(PREFIX + '/claim', json={**body, 'token': other['token']})).status_code == 422
    assert (await api.post(PREFIX + '/claim', json=body)).status_code == 200


async def test_owner_cookie_cannot_bind_a_members_identity_and_member_listing_is_scoped(api, mobile, member_accounts):
    from helpers import verified_post
    from test_users import browser, join
    invitation = await verified_post(api, '/api/users/invites', {'role': 'member', 'account': 'dana-agent'})
    assert invitation.status_code == 201, invitation.text
    async with browser(api) as member:
        assert (await join(member, invitation.json()['link'], 'alex')).status_code == 200
        credential, _, _, _ = await issue(api, mobile, await begin(api, mobile, person='alex'))
        await activate(api)
        member_devices = (await member.get(PREFIX + '/devices')).json()['devices']
        assert len(member_devices) == 1 and member_devices[0]['person'] == 'alex'
        api._transport.client = None
        headers = {**mobile, 'Host': 'remote.example.com', 'X-Acc-Edge-Client': devices.fingerprint(
            x509.load_pem_x509_certificate(credential['certificate'].encode()))}
        assert (await api.post(PREFIX + '/session/bind', headers=headers, json={})).status_code == 401


async def test_native_possession_needs_an_unforgeable_edge_assertion(api, mobile):
    api.headers.pop('X-Acc-Native-Edge')
    credential, _, _, _ = await issue(api, mobile)
    await activate(api)
    api._transport.client = None
    headers = {**mobile, 'Host': 'remote.example.com', 'X-Acc-Edge-Client': devices.fingerprint(
        x509.load_pem_x509_certificate(credential['certificate'].encode()))}
    result = await api.post(PREFIX + '/session/bind', headers=headers, json={})
    assert result.status_code == 403, result.text
    assert result.json() == {'detail': 'Native identity requires the trusted public edge.'}  # JSON, as every refusal


@pytest.mark.parametrize('condition', ['missing', 'symlink', 'readable', 'oversized', 'wrong-key', 'duplicate'])
async def test_native_assertion_fails_closed_for_unsafe_key_or_header(api, mobile, condition):
    credential, _, _, _ = await issue(api, mobile)
    await activate(api)
    path = api.app.state.ctx.settings.data_dir / 'secrets/mobile-edge-key'
    if condition == 'missing':
        path.unlink()
    elif condition == 'symlink':
        target = path.with_name('test-target')
        path.rename(target)
        path.symlink_to(target)
    elif condition == 'readable':
        path.chmod(0o644)
    elif condition == 'oversized':
        path.write_text(api.headers['X-Acc-Native-Edge'] + ' ' * 1000)
    elif condition == 'wrong-key':
        api.headers['X-Acc-Native-Edge'] = 'f' * 64
    api._transport.client = None
    headers = list(mobile.items()) + [('Host', 'remote.example.com'), ('X-Acc-Edge-Client', devices.fingerprint(
        x509.load_pem_x509_certificate(credential['certificate'].encode())))]
    if condition == 'duplicate':
        headers += [('X-Acc-Native-Edge', api.headers['X-Acc-Native-Edge']), ('X-Acc-Native-Edge', api.headers['X-Acc-Native-Edge'])]
    assert (await api.post(PREFIX + '/session/bind', headers=headers, json={})).status_code == 403


async def test_assertion_is_consumed_before_handlers_and_key_rotation_closes_stream(api, mobile):
    import asyncio

    from acc.server import edge
    credential, _, _, _ = await issue(api, mobile)
    await activate(api)
    fingerprint = devices.fingerprint(x509.load_pem_x509_certificate(credential['certificate'].encode()))
    received, inside = asyncio.Event(), asyncio.Event()
    captured = []
    async def downstream(scope, receive, send):
        captured.extend(scope['headers'])
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        inside.set()
        assert (await receive())['type'] == 'http.disconnect'
        received.set()
    scope = {'type': 'http', 'path': '/api/projects', 'client': None, 'headers': [
        (b'host', b'remote.example.com'), (b'x-acc-edge-client', fingerprint.encode()),
        (b'x-acc-native-edge', api.headers['X-Acc-Native-Edge'].encode())]}
    async def send(message):
        pass
    task = asyncio.create_task(edge.EdgeGuard(downstream, api.app.state.ctx)(scope, asyncio.Queue().get, send))
    try:
        await asyncio.wait_for(inside.wait(), 5)
        assert all(name != b'x-acc-native-edge' for name, value in captured)
        path = api.app.state.ctx.settings.data_dir / 'secrets/mobile-edge-key'
        path.write_text(secrets.token_hex(32))
        assert await api.app.state.ctx.edge.sweep() == 1
        await asyncio.wait_for(received.wait(), 5)
        await asyncio.wait_for(task, 5)
        assert api.app.state.ctx.edge.open == set()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_native_edge_never_accepts_a_portable_or_another_persons_cookie_on_ordinary_routes(api, mobile, member_accounts):
    """Independent review: possession of one person's phone never upgrades a cookie that is not bound to it."""
    from helpers import verified_post
    from test_users import browser, join
    invitation = await verified_post(api, '/api/users/invites', {'role': 'member', 'account': 'dana-agent'})
    assert invitation.status_code == 201, invitation.text
    async with browser(api) as member:
        assert (await join(member, invitation.json()['link'], 'alex')).status_code == 200
        owner_phone, _, _, _ = await issue(api, mobile)
        member_phone, _, _, _ = await issue(api, mobile, await begin(api, mobile, person='alex'), install='c' * 32)
        await activate(api)
        api._transport.client = None
        def through(credential):
            return {**mobile, 'Host': 'remote.example.com', 'X-Acc-Edge-Client': devices.fingerprint(
                x509.load_pem_x509_certificate(credential['certificate'].encode()))}
        # The owner's ordinary (portable) browser cookie: refused through either phone until explicitly bound.
        assert (await api.get('/api/projects', headers=through(owner_phone))).status_code == 401
        assert (await api.get('/api/projects', headers=through(member_phone))).status_code == 401
        assert (await api.get('/api/auth/state', headers=through(member_phone))).json().get('authenticated') is not True
        # Bound to the owner's phone, it still never works through the member's phone.
        assert (await api.post(PREFIX + '/session/bind', headers=through(owner_phone), json={})).status_code == 200
        assert (await api.get('/api/projects', headers=through(owner_phone))).status_code == 200
        assert (await api.get('/api/projects', headers=through(member_phone))).status_code == 401


def refused_rows(api, operation=None):
    with api.app.state.db() as s:
        rows = list(s.scalars(select(AuditLog).where(AuditLog.action == 'mobile.refused').order_by(AuditLog.id)))
        return [(r.actor, r.target, r.detail) for r in rows if operation is None or r.detail['operation'] == operation]


async def test_refused_attempts_are_audited_without_secrets_and_capped_per_client(api, mobile, member_accounts):
    from helpers import verified_post
    from test_users import browser, csrf, join
    pending = await begin(api, mobile)
    body = claim_body(pending)
    assert (await api.post(PREFIX + '/claim', json={**body, 'proof': 'x' * 80})).status_code == 422
    (actor, _, detail), = refused_rows(api, 'claim')
    assert actor == 'anonymous' and detail['status'] == 422 and detail['reason'] == "The app's pairing request could not be verified. Start pairing again."
    assert body['token'] not in str(detail) and body['csr'] not in str(detail)
    # An owner without a recent verification, and a member, are refused and audited too.
    with write_db(api.app.state.db) as s:
        s.get(AuthSession, auth.digest(api.cookies.get('acc_session'))).reauthenticated_at = auth.now() - timedelta(minutes=6)
        s.commit()
    assert (await api.post(PREFIX + '/devices/' + '0' * 32 + '/revoke', headers=mobile, json={})).status_code == 403
    assert refused_rows(api, 'revoke')[-1][:2] == ('casey', '0' * 32)
    invitation = await verified_post(api, '/api/users/invites', {'role': 'member', 'account': 'dana-agent'})
    async with browser(api) as member:
        assert (await join(member, invitation.json()['link'], 'alex')).status_code == 200
        member.headers['Host'] = 'acc.example.lan'
        assert (await member.post(PREFIX + '/pairings', headers=csrf(member), json={'person': 'alex', 'label': 'P'})).status_code == 403
    assert refused_rows(api, '/pairings')[-1][0] == 'alex'
    # A flood from one client leaves at most ten rows in the window, 429s included.
    for _ in range(25):
        await api.post(PREFIX + '/credential', json={'receipt': 'x' * 43})
    client = refused_rows(api)[0][2]['client']
    assert len([r for r in refused_rows(api) if r[2]['client'] == client]) == 10


async def test_owner_successes_do_not_count_and_device_attempts_have_their_own_bucket(api, mobile):
    # Ten unauthenticated receiver attempts from this address use up only the device bucket.
    for _ in range(10):
        await api.post(PREFIX + '/credential', json={'receipt': 'x' * 43})
    assert (await api.post(PREFIX + '/credential', json={'receipt': 'x' * 43})).status_code == 429
    # Twelve successful starts: an owner's successful steps never count.
    for _ in range(12):
        await begin(api, mobile)
        with write_db(api.app.state.db) as s:
            s.execute(delete(DevicePairing))
            s.commit()
    # Ten failed confirmations do count, and then even a valid one waits.
    for _ in range(10):
        assert (await api.post(PREFIX + '/pairings/' + '0' * 32 + '/confirm', headers=mobile,
                               json={'matching_code': '000000'})).status_code == 410
    assert (await api.post(PREFIX + '/pairings', headers=mobile, json={'person': 'casey', 'label': 'P'})).status_code == 429


async def test_public_listing_shows_a_short_installation_fingerprint_not_the_installation_id(api, mobile):
    credential, _, _, _ = await issue(api, mobile)
    private = (await api.get(PREFIX + '/devices')).json()['devices'][0]
    assert private['installation_id'] == INSTALL and private['installation'] == auth.digest(INSTALL)[:12]
    await activate(api)
    api._transport.client = None
    headers = {**mobile, 'Host': 'remote.example.com', 'X-Acc-Edge-Client': devices.fingerprint(
        x509.load_pem_x509_certificate(credential['certificate'].encode()))}
    assert (await api.post(PREFIX + '/session/bind', headers=headers, json={})).status_code == 200
    public = (await api.get(PREFIX + '/devices', headers=headers)).json()['devices'][0]
    assert 'installation_id' not in public and INSTALL not in str(public)
    assert public['installation'] == private['installation']


async def test_waiting_for_the_owner_never_throttles_the_phone(api, mobile):
    """The client polls /credential "in a few seconds" until the owner types the code; a live pairing's own receipt
    must not use up the device bucket before its five-minute expiry. Unknown receipts still count."""
    pending = await begin(api, mobile)
    claimed = await api.post(PREFIX + '/claim', json=claim_body(pending))
    assert claimed.status_code == 200, claimed.text
    for _ in range(15):
        assert (await api.post(PREFIX + '/credential', json={'receipt': claimed.json()['receipt']})).status_code == 409
    confirmed = await api.post(PREFIX + '/pairings/' + pending['id'] + '/confirm', headers=mobile,
                               json={'matching_code': claimed.json()['matching_code']})
    assert confirmed.status_code == 200, confirmed.text
    assert (await api.post(PREFIX + '/credential', json={'receipt': claimed.json()['receipt']})).status_code == 200
    for _ in range(9):  # the claim counted once
        assert (await api.post(PREFIX + '/credential', json={'receipt': 'x' * 43})).status_code == 410
    assert (await api.post(PREFIX + '/credential', json={'receipt': 'x' * 43})).status_code == 429


async def test_pairing_off_says_who_turns_it_on(api, mobile, monkeypatch):
    original = devices.settings
    monkeypatch.setattr(devices, 'settings', lambda *a, **kw: {**original(*a, **kw), 'pairing_enabled': False})
    result = await api.post(PREFIX + '/pairings', headers=mobile, json={'person': 'casey', 'label': 'P'})
    assert result.status_code == 409
    assert 'Ask an owner to turn on LAN pairing in Settings → Remote access' in result.json()['detail']


async def test_a_bound_session_ends_when_its_leaf_is_retired(api, mobile, monkeypatch):
    # lookup_session checked status and expiry by hand, without the rotation's retire_after that the edge applies.
    from datetime import timedelta

    from helpers import TOTP_AT
    from test_auth import PASSWORD

    from acc.server.models import Device, User
    credential, _, _, _ = await issue(api, mobile)
    await activate(api)
    api._transport.client = None
    fingerprint = devices.fingerprint(x509.load_pem_x509_certificate(credential['certificate'].encode()))
    api.headers.update({'Host': 'remote.example.com', 'X-Acc-Edge-Client': fingerprint})
    with api.app.state.db() as s:
        secret = s.scalar(select(User.totp_secret).where(User.username == 'casey'))
    monkeypatch.setattr(auth.time, 'time', lambda: TOTP_AT + 60)
    signed = await api.post('/api/auth/login', headers={'Origin': mobile['Origin']},
                            json={'username': 'casey', 'password': PASSWORD, 'code': auth.totp(secret, TOTP_AT + 60)})
    assert signed.status_code == 200, signed.text
    headers = {**mobile, 'X-ACC-CSRF': api.cookies.get('acc_csrf')}
    assert (await api.post(PREFIX + '/session/bind', headers=headers, json={})).status_code == 200
    cookie = api.cookies.get('acc_session')
    with write_db(api.app.state.db) as s:
        assert auth.lookup_session(s, cookie, edge_fingerprint=fingerprint) is not None
        device = s.scalar(select(Device).where(Device.fingerprint == fingerprint))
        device.retire_after = auth.now() - timedelta(seconds=1)
        s.commit()
        assert auth.lookup_session(s, cookie, edge_fingerprint=fingerprint) is None


async def test_every_lifecycle_response_is_no_store(api, mobile):
    # Set once by the router dependency, for every handler: codes, receipts and certificates are never cached.
    started = await api.post(PREFIX + '/pairings', headers=mobile, json={'person': 'casey', 'label': 'P'})
    claimed = await api.post(PREFIX + '/claim', json=claim_body(started.json()))
    confirmed = await api.post(PREFIX + '/pairings/' + started.json()['id'] + '/confirm', headers=mobile,
                               json={'matching_code': claimed.json()['matching_code']})
    issued = await api.post(PREFIX + '/credential', json={'receipt': claimed.json()['receipt']})
    rotated = await api.post(PREFIX + '/devices/' + issued.json()['device_id'] + '/rotate', headers=mobile,
                             json={'person': 'casey', 'label': 'P'})
    listed = await api.get(PREFIX + '/devices', headers=mobile)
    for response in (started, claimed, confirmed, issued, rotated, listed):
        assert response.status_code == 200, response.text
        assert response.headers['cache-control'] == 'no-store', response.request.url


@pytest.mark.parametrize('change', ['consent-off', 'native-key-rotation', 'feature-off'])
async def test_mixed_edge_streams_keep_mobile_and_git_consent_gates(api, project, mobile, monkeypatch, change):
    from test_public_git_backup_policy import FRESH, LEAF, prepare

    from acc.server import edge
    from acc.server.models import Node

    credential, _, _, _ = await issue(api, mobile)
    fingerprint = devices.fingerprint(x509.load_pem_x509_certificate(credential['certificate'].encode()))
    prepare(api, project)
    with write_db(api.app.state.db) as s:
        s.get(Node, FRESH).public_git_backup = True
        s.commit()
    ctx = api.app.state.ctx
    native = edge.Connection(fingerprint, '/api/projects')
    native.native_key_hash = auth.digest(api.headers['X-Acc-Native-Edge'])
    git = edge.Connection(LEAF, f"/api/git/{project['id']}.git/git-receive-pack")
    control = edge.Connection(LEAF, '/api/nodes/connect')
    ctx.edge.open.update([native, git, control])
    try:
        assert await ctx.edge.sweep() == 0
        assert not any(c.closed.is_set() for c in (native, git, control))
        if change == 'consent-off':
            with write_db(api.app.state.db) as s:
                s.get(Node, FRESH).public_git_backup = False
                s.commit()
        elif change == 'native-key-rotation':
            (ctx.settings.data_dir / 'secrets/mobile-edge-key').write_text(secrets.token_hex(32))
        else:
            monkeypatch.setattr(ctx, 'settings', replace(ctx.settings, mobile_enabled=False))
        assert await ctx.edge.sweep() == 1
        assert git.closed.is_set() == (change == 'consent-off')
        assert native.closed.is_set() == (change != 'consent-off')
        assert not control.closed.is_set()
    finally:
        ctx.edge.open.difference_update([native, git, control])


async def test_rollover_status_counts_native_identities_only_while_mobile_is_enabled(api, mobile, monkeypatch, tmp_path):
    await issue(api, mobile)
    ca = helper()
    ca.initialize(tmp_path / 'next-authority')
    monkeypatch.setattr(devices, 'sign', lambda body: ca.issue(tmp_path / 'next-authority', body))
    ctx = api.app.state.ctx
    ctx.edge.forget_authority()
    status = await api.get('/api/remote-access', headers=mobile)
    assert status.status_code == 200, status.text
    assert status.json()['rollover']['previous_identities'] == 1
    monkeypatch.setattr(ctx, 'settings', replace(ctx.settings, mobile_enabled=False))
    status = await api.get('/api/remote-access', headers=mobile)
    assert status.json()['rollover']['previous_identities'] == 0


async def test_native_admission_halts_before_runtime_state_is_cleared(api, mobile):
    credential, _, _, _ = await issue(api, mobile)
    await activate(api)
    api._transport.client = None
    headers = {**mobile, 'Host': 'remote.example.com', 'X-Acc-Edge-Client': devices.fingerprint(
        x509.load_pem_x509_certificate(credential['certificate'].encode()))}
    assert (await api.post(PREFIX + '/session/bind', headers=headers, json={})).status_code == 200
    assert (await api.get('/api/projects', headers=headers)).status_code == 200
    api.app.state.ctx.edge.halted = True
    assert (await api.get('/api/projects', headers=headers)).status_code == 403
