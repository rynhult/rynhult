"""Owner review/commit boundary and durable registry reconciliation."""
import pytest
from helpers import write_db
from sqlalchemy import select
from test_devices import enable, start
from test_passkeys import setup
from test_remote_access_helper import edge as _edge

from acc.server import remote_access
from acc.server.models import AuditLog, Device, Setting

edge = _edge  # Register the shared pytest fixture without treating function parameters as reimports.


@pytest.fixture
def installed(monkeypatch, edge):
    helper, _, issued, _, _ = edge
    async def request(body):
        return helper.handle(body)
    monkeypatch.setattr(remote_access, 'request', request)
    return helper, issued


def add_device(api, issued):
    from cryptography import x509

    from acc.server import devices
    cert = x509.load_pem_x509_certificate(issued['certificate'].encode())
    with write_db(api.app.state.db) as s:
        s.add(Device(id='a' * 32, person='owner', label='Phone', kind='browser', status='active',
                     certificate_pem=issued['certificate'], fingerprint=devices.fingerprint(cert),
                     serial=str(cert.serial_number), expires_at=cert.not_valid_after_utc))
        s.commit()


async def test_owner_reviews_exact_configuration_applies_and_turns_off(api, env, monkeypatch, installed):
    headers, _ = await setup(api, env, monkeypatch)
    await enable(api, headers)
    helper, issued = installed
    add_device(api, issued)
    # setup's person is not necessarily named owner: bind to the actual signed-in person.
    from acc.server.models import User
    with write_db(api.app.state.db) as s:
        s.get(Device, 'a' * 32).person = s.query(User).filter_by(role='owner').first().username
        s.commit()
    review = await api.get('/api/remote-access/review')
    assert review.status_code == 200, review.text
    assert 'unix//run/accd.sock' in str(review.json()['configuration'])
    assert review.json()['devices_allowed'] == 1
    applied = await api.post('/api/remote-access/apply', headers=headers, json={'revision': review.json()['revision']})
    assert applied.status_code == 200, applied.text
    assert applied.json()['enabled'] is True
    state = (await api.get('/api/remote-access')).json()
    assert state['enabled'] is True and state['last_applied_revision'] == review.json()['revision']
    assert state['devices_allowed'] == 1 and state['last_reload_result'] == 'verified'
    # Off never depends on a hostname, CA, pairing being on, or recent reauthentication.
    from acc.server import auth
    from acc.server.models import AuthSession
    with write_db(api.app.state.db) as s:
        s.get(AuthSession, auth.digest(api.cookies.get('acc_session'))).reauthenticated_at = None
        s.get(Setting, 'remote_access').value = {}
        s.commit()
    helper.ca = 'unavailable'
    result = await api.post('/api/remote-access/off', headers=headers, json={})
    assert result.status_code == 200 and result.json()['enabled'] is False


async def test_missing_helper_is_visible_and_never_reports_on(api, env, monkeypatch):
    async def unavailable(message):
        raise OSError('test helper is not installed')
    monkeypatch.setattr(remote_access, 'request', unavailable)
    headers, _ = await setup(api, env, monkeypatch)
    await enable(api, headers)
    result = await api.post('/api/remote-access/apply', headers=headers, json={'revision': 'a' * 64})
    assert result.status_code == 503
    state = (await api.get('/api/remote-access')).json()
    assert state['enabled'] is not True and state['last_reload_result'] == 'never'
    off = await api.post('/api/remote-access/off', headers=headers, json={})
    assert off.status_code == 503
    assert (await api.get('/api/remote-access')).json()['pending'] is True


async def test_changed_registry_refuses_old_review(api, env, monkeypatch, installed):
    headers, _ = await setup(api, env, monkeypatch)
    await enable(api, headers)
    review = (await api.get('/api/remote-access/review')).json()
    installed[0].policy['hostnames'].append('other.example.com')
    await api.put('/api/remote-access', headers=headers, json={
        'pairing_enabled': True, 'public_hostname': 'other.example.com', 'private_origin': 'https://acc.example.lan', 'certificate_days': 90})
    result = await api.post('/api/remote-access/apply', headers=headers, json={'revision': review['revision']})
    assert result.status_code == 409
    assert (await api.get('/api/remote-access')).json()['enabled'] is False


async def test_matching_code_is_typed_from_the_new_device_and_single_use(api, env, monkeypatch):
    # Only the receiving device shows the code. The owner must read it there and type it;
    # an owner page that shows the code and posts it back would check nothing.
    headers, _ = await setup(api, env, monkeypatch)
    await enable(api, headers)
    pending = await start(api, headers)
    claim = (await api.post('/api/device-pairing/claim', json={'token': pending['link'].split('#')[1]})).json()
    code = claim['matching_code']
    assert len(code) == 6 and code.isdigit()
    detail = (await api.get('/api/devices/pairings/' + pending['id'])).json()
    assert 'matching_code' not in detail and code not in str(detail)
    listed = (await api.get('/api/devices/pairings')).json()['pairings'][0]
    assert 'matching_code' not in listed and code not in str(listed)
    url = '/api/devices/pairings/' + pending['id'] + '/confirm'
    assert (await api.post(url, headers=headers, json={})).status_code == 422
    assert (await api.post(url, headers=headers, json={'matching_code': 'x' * 6})).status_code == 422
    wrong = '000000' if code != '000000' else '111111'
    assert (await api.post(url, headers=headers, json={'matching_code': wrong})).status_code == 409
    # A mismatch ends this pairing: no second guess, start again.
    assert (await api.post(url, headers=headers, json={'matching_code': code})).status_code == 410
    assert (await api.get('/api/devices')).json()['devices'] == []
    with api.app.state.db() as s:
        audit = s.scalars(select(AuditLog).where(AuditLog.action == 'device.pairing_mismatch')).all()
    assert len(audit) == 1 and audit[0].target == pending['id']


async def test_registry_reapply_rejects_revoked_device_in_disposable_caddy(api, env, monkeypatch, tmp_path):
    import base64
    import json
    import os
    import subprocess
    import sys
    import time
    from pathlib import Path

    from cryptography.hazmat.primitives import serialization

    from acc.remote_access_helper import RemoteAccessHelper
    from acc.server.models import User

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
    from remote_access_qualification.harness import Lab
    headers, _ = await setup(api, env, monkeypatch)
    await enable(api, headers)
    with Lab() as lab:
        base = lab.config([])
        server = base['apps']['http']['servers']['qualification']
        server['tls_connection_policies'].pop(0)
        server['routes'].pop(0)
        lab._admin('POST', '/load', base)
        folder = tmp_path / 'edge'
        folder.mkdir(mode=0o700)
        fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
        events = []
        def runner(action, config):
            events.append(action)
            if action == 'validate':
                path = folder / 'validate.json'
                path.write_text(json.dumps(config))
                return subprocess.run(['/usr/bin/caddy', 'validate', '--config', str(path)],
                    env=lab.env, capture_output=True).returncode == 0
            if action == 'reload':
                lab._admin('POST', '/load', config)
                return True
            return json.loads(lab._admin('GET', '/config/')) == config
        try:
            helper = RemoteAccessHelper(fd, {'qualified': True, 'hostnames': ['remote.example.com'], 'server': 'qualification'},
                base, (lab.root / 'device_ca.pem').read_text(), runner, owner=os.getuid(),
                upstream=f'127.0.0.1:{lab.upstream.server_port}')
            async def request(body):
                return helper.handle(body)
            monkeypatch.setattr(remote_access, 'request', request)
            cert = lab.certs['active']
            with write_db(api.app.state.db) as s:
                person = s.query(User).filter_by(role='owner').first().username
                s.add(Device(id='a' * 32, person=person, label='Phone', kind='browser', status='active',
                    certificate_pem=cert.public_bytes(serialization.Encoding.PEM).decode(),
                    fingerprint=base64.b16encode(cert.signature).decode()[:64], serial=str(cert.serial_number),
                    expires_at=cert.not_valid_after_utc))
                s.commit()
            review = (await api.get('/api/remote-access/review')).json()
            result = await api.post('/api/remote-access/apply', headers=headers, json={'revision': review['revision']})
            assert result.status_code == 200, result.text
            for path in ('/pair-device', '/api/device-pairing/claim', '/api/devices/pairings', '/api/nodes/connect', '/preview/probe'):
                assert lab.request('active', path=path) == 403
            assert lab.count == 0
            assert lab.request('active', path='/api/devices') == 200
            assert lab.request('active') == 200
            before = time.monotonic()
            result = await api.post('/api/devices/' + 'a' * 32 + '/revoke', headers=headers, json={})
            assert result.status_code == 200 and not result.json()['pending'], result.text
            assert time.monotonic() - before < 60
            for tls in ('TLSv1_2', 'TLSv1_3'):
                assert lab.request('active', tls=tls) is None
            assert lab.count == 2
            assert events == ['verify', 'validate', 'reload', 'verify', 'verify', 'verify', 'validate', 'reload', 'verify']
            assert lab.request(None, sni='acc.example.lan', host='acc.example.lan') == 200
            # Person removal, expiry, and direct registry changes are also picked up by the worker's tick.
            with write_db(api.app.state.db) as s:
                s.get(Device, 'a' * 32).status = 'active'
                s.commit()
            await api.app.state.ctx.remote_access.reconcile()
            assert lab.request('active') == 200
            with write_db(api.app.state.db) as s:
                person_row = s.query(User).filter_by(username=person).first()
                s.delete(person_row)
                s.commit()
            await api.app.state.ctx.remote_access.reconcile()
            assert lab.request('active') is None
            assert helper.handle({'action': 'status'})['devices_allowed'] == 0
            await api.app.state.ctx.remote_access.change(False, 'system')
            assert lab.request('active') is None
            assert lab.request(None, sni='acc.example.lan', host='acc.example.lan') == 200
        finally:
            os.close(fd)


async def test_reconciler_disables_instead_of_reenabling_after_edge_restart(api, env, monkeypatch, installed):
    headers, _ = await setup(api, env, monkeypatch)
    await enable(api, headers)
    helper, _ = installed
    review = (await api.get('/api/remote-access/review')).json()
    result = await api.post('/api/remote-access/apply', headers=headers, json={'revision': review['revision']})
    assert result.status_code == 200
    old = remote_access.request
    async def drift(body):
        if body == {'action': 'status'}:
            return {'enabled': None, 'last_reload_result': 'configuration drift'}
        return await old(body)
    monkeypatch.setattr(remote_access, 'request', drift)
    await api.app.state.ctx.remote_access.reconcile()
    state = (await api.get('/api/remote-access')).json()
    assert state['enabled'] is False and state['desired_enabled'] is False
    assert helper.handle({'action': 'status'})['enabled'] is False


async def test_removing_a_device_reconciles_the_allow_list(api, env, monkeypatch, installed):
    from acc.server.models import User
    headers, _ = await setup(api, env, monkeypatch)
    await enable(api, headers)
    helper, issued = installed
    add_device(api, issued)
    with write_db(api.app.state.db) as s:
        s.get(Device, 'a' * 32).person = s.query(User).filter_by(role='owner').first().username
        s.commit()
    review = (await api.get('/api/remote-access/review')).json()
    assert (await api.post('/api/remote-access/apply', headers=headers, json={'revision': review['revision']})).status_code == 200
    result = await api.delete('/api/devices/' + 'a' * 32, headers=headers)
    assert result.status_code == 200, result.text
    with api.app.state.db() as s:
        assert s.get(Device, 'a' * 32) is None
    assert helper.handle({'action': 'status'})['devices_allowed'] == 0


async def test_removing_a_person_reapplies_before_return(api, env, monkeypatch, installed):
    from acc.server.models import User
    headers, _ = await setup(api, env, monkeypatch)
    await enable(api, headers)
    helper, issued = installed
    add_device(api, issued)
    with write_db(api.app.state.db) as s:
        user = User(username='morgan', role='member', account='dana-agent', password_hash='x', totp_secret='x')
        s.add(user)
        s.get(Device, 'a' * 32).person = 'morgan'
        s.flush()
        user_id = user.id
        s.commit()
    review = (await api.get('/api/remote-access/review')).json()
    assert (await api.post('/api/remote-access/apply', headers=headers, json={'revision': review['revision']})).status_code == 200
    assert helper.handle({'action': 'status'})['devices_allowed'] == 1
    result = await api.post(f'/api/users/{user_id}/remove', headers=headers, json={'resources': 'transfer'})
    assert result.status_code == 200, result.text
    assert helper.handle({'action': 'status'})['devices_allowed'] == 0
    with api.app.state.db() as s:
        assert s.get(Device, 'a' * 32).status == 'revoked'


async def test_fixed_socket_client_checks_peer_and_bounded_real_round_trip(monkeypatch, edge):
    import asyncio
    import tempfile
    from pathlib import Path
    helper, _, issued, _, _ = edge
    original = asyncio.open_unix_connection
    with tempfile.TemporaryDirectory(prefix='acc-m12-ipc-', dir='/tmp') as folder:
        endpoint = str(Path(folder) / 'helper.sock')
        async def respond(reader, writer):
            try:
                raw = await reader.readline()
                if raw:
                    import json
                    writer.write(json.dumps(helper.handle(json.loads(raw))).encode() + b'\n')
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_unix_server(respond, endpoint)
        async def connect(path, **kwargs):
            assert path == '/run/acc-remote-access.sock'
            return await original(endpoint, **kwargs)
        monkeypatch.setattr(remote_access.asyncio, 'open_unix_connection', connect)
        try:
            monkeypatch.setattr(remote_access, 'peer_uid', lambda sock: 1234)
            with pytest.raises(OSError, match='untrusted'):
                await remote_access.request({'action': 'status'})
            monkeypatch.setattr(remote_access, 'peer_uid', lambda sock: 0)
            review = await remote_access.request({'action': 'review', 'hostname': 'remote.example.com', 'leaves': [issued['certificate']]})
            assert review['devices_allowed'] == 1 and len(review['revision']) == 64
            state = await remote_access.request({'action': 'off'})
            assert state['enabled'] is False and state['last_reload_result'] == 'verified'
        finally:
            server.close()
            await server.wait_closed()


async def test_failed_apply_reports_verified_off_recovery_without_claiming_pending(api, env, monkeypatch, installed):
    headers, _ = await setup(api, env, monkeypatch)
    await enable(api, headers)
    review = (await api.get('/api/remote-access/review')).json()
    original = remote_access.request
    async def fail_apply(body):
        if body['action'] == 'apply':
            raise OSError('unavailable')
        return await original(body)
    monkeypatch.setattr(remote_access, 'request', fail_apply)
    result = await api.post('/api/remote-access/apply', headers=headers, json={'revision': review['revision']})
    assert result.status_code == 503
    assert 'turned it off again and checked that it is off' in result.json()['detail']
    state = (await api.get('/api/remote-access')).json()
    assert state['enabled'] is False and state['pending'] is False
    assert state['desired_enabled'] is False


async def test_device_list_marks_unapplied_revocation_pending(api, env, monkeypatch, installed):
    from acc.server.models import User
    headers, _ = await setup(api, env, monkeypatch)
    await enable(api, headers)
    _, issued = installed
    add_device(api, issued)
    with write_db(api.app.state.db) as s:
        s.get(Device, 'a' * 32).person = s.query(User).filter_by(role='owner').first().username
        s.commit()
    review = (await api.get('/api/remote-access/review')).json()
    assert (await api.post('/api/remote-access/apply', headers=headers, json={'revision': review['revision']})).status_code == 200
    with write_db(api.app.state.db) as s:
        s.get(Device, 'a' * 32).status = 'revoked'
        s.commit()
    listing = (await api.get('/api/devices')).json()
    assert listing['policy_pending'] is True
    await api.app.state.ctx.remote_access.reconcile()
    assert (await api.get('/api/devices')).json()['policy_pending'] is False
