"""Rollover must never revive removed leaves or deliver data after invalidation."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from cryptography import x509
from helpers import write_db
from test_device_ca_machine import csr
from test_device_ca_rollover import pem, staged

from acc.server import auth, devices, edge
from acc.server.db import make_engine, make_sessionmaker, migrate
from acc.server.models import Device, Setting, User

KEY = 'e' * 64


@pytest.fixture
def world(tmp_path):
    ca, state, helper_dir, _, _ = staged(tmp_path)
    def issue(kind, number):
        body = {'operation': 'issue', 'device_id': str(number) * 32, 'days': 90}
        if kind == 'native':
            body.update(operation='issue_native', csr=csr()[1])
        certificate = ca.issue(state, body)['certificate']
        return devices.fingerprint(x509.load_pem_x509_certificate(certificate.encode())), certificate
    old = {kind: issue(kind, i) for i, kind in enumerate(('browser', 'native'), 1)}
    ca.rollover_promote(state, helper_dir)
    fresh = {kind: issue(kind, i) for i, kind in enumerate(('browser', 'native'), 3)}
    url = f'sqlite:///{tmp_path / "review.db"}'
    migrate(url)
    engine = make_engine(url)
    db = make_sessionmaker(engine)
    ctx = SimpleNamespace(db=db, settings=SimpleNamespace(mobile_enabled=True, data_dir=tmp_path))
    ctx.edge = edge.Edge(ctx)
    ctx.review_authority = pem(state)
    secret = tmp_path / 'secrets/mobile-edge-key'
    secret.parent.mkdir()
    secret.write_text(KEY)
    secret.chmod(0o600)
    with write_db(db) as s:
        s.add(User(username='casey', password_hash='test-only', totp_secret='test-only'))
        s.add(Setting(key='remote_access', value={'public_hostname': 'remote.example.com'}))
        s.add(Setting(key='remote_access_runtime', value={'desired_enabled': True}))
        for kind, (fingerprint, certificate) in old.items():
            s.add(Device(id=kind, person='casey', kind=kind, label=kind, status='active',
                certificate_pem=certificate, fingerprint=fingerprint, serial=kind,
                expires_at=auth.now() + timedelta(days=1)))
        s.commit()
    try:
        with db() as s:
            assert ctx.edge.rollover(s, pem(state))['previous_identities'] == 2
        for fingerprint, _ in old.values():
            assert ctx.edge.check(fingerprint, '/api/projects')
        yield ctx, {kind: value[0] for kind, value in old.items()}, fresh
    finally:
        engine.dispose()


@pytest.mark.parametrize('change,transport', [(kind + '-' + change, transport) for kind in ('browser', 'native')
    for change in ('revoke', 'rotate') for transport in ('http', 'websocket')])
async def test_completed_invalidation_wins_over_a_simultaneously_ready_body(world, change, transport):
    ctx, fingerprints, fresh = world
    body = asyncio.Queue()
    waiting = asyncio.Event()
    delivered, sent = [], []
    connected = False

    async def receive():
        nonlocal connected
        if transport == 'websocket' and not connected:
            connected = True
            return {'type': 'websocket.connect'}
        waiting.set()
        return await body.get()

    async def handler(scope, guarded_receive, send):
        if transport == 'websocket':
            assert await guarded_receive() == {'type': 'websocket.connect'}
            await send({'type': 'websocket.accept'})
        delivered.append(await guarded_receive())

    async def send(message):
        sent.append(message)

    native = change.startswith('native')
    kind = 'native' if native else 'browser'
    headers = [(edge.HEADER, fingerprints[kind].encode()), (b'host', b'remote.example.com')]
    if native:
        headers.append((b'x-acc-native-edge', KEY.encode()))
    task = asyncio.create_task(edge.EdgeGuard(handler, ctx)(
        {'type': transport, 'path': '/api/projects', 'headers': headers, 'client': None}, receive, send))
    try:
        await asyncio.wait_for(waiting.wait(), 5)
        with write_db(ctx.db) as s:
            row = s.get(Device, kind)
            if change.endswith('revoke'):
                row.status = 'revoked'
            else:
                row.fingerprint = fresh[kind][0]
                row.certificate_pem = fresh[kind][1]
            s.commit()
        assert ctx.edge.check(fingerprints[kind], '/api/projects') is None
        other = 'browser' if native else 'native'
        assert ctx.edge.check(fingerprints[other], '/api/projects')
        if change.endswith('rotate'):
            assert ctx.edge.check(fresh[kind][0], '/api/projects')
        with ctx.db() as s:
            assert ctx.edge.rollover(s, ctx.review_authority)['previous_identities'] == 1
        assert await ctx.edge.sweep() == 1
        # Invalidation has completed BEFORE these bytes arrive. Both waiter tasks can
        # finish in the same loop turn; a ready body must not outrank revocation.
        body.put_nowait({'type': 'http.request', 'body': b'after-invalidation', 'more_body': False}
                        if transport == 'http' else {'type': 'websocket.receive', 'bytes': b'after-invalidation'})
        await asyncio.wait_for(task, 5)
        assert delivered == ([{'type': 'http.disconnect'}] if transport == 'http'
                             else [{'type': 'websocket.disconnect', 'code': 4403}])
        if transport == 'websocket':
            assert sent == [{'type': 'websocket.accept'}, {'type': 'websocket.close', 'code': 4403}]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert not ctx.edge.open
