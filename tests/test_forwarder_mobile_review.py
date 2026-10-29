"""Independent checks of the path-consent/native-key merge and the 0068 mobile-data round trip."""
import asyncio
import sqlite3
from contextlib import closing
from datetime import timedelta
from types import SimpleNamespace

import pytest
from alembic import command
from helpers import write_db

from acc.server import auth, edge
from acc.server.db import alembic_config, make_engine, make_sessionmaker, migrate
from acc.server.models import AuthSession, Device, DevicePairing, Node, Project, Setting, User

NODE = 'a' * 32
MACHINE = 'b' * 64
NATIVE = 'c' * 64
BROWSER = 'd' * 64
KEY = 'e' * 64
GIT = '/api/git/1.git/git-receive-pack'


@pytest.fixture
def world(tmp_path):
    path = tmp_path / 'review.db'
    migrate(f'sqlite:///{path}')
    engine = make_engine(f'sqlite:///{path}')
    db = make_sessionmaker(engine)
    ctx = SimpleNamespace(db=db, settings=SimpleNamespace(mobile_enabled=True, data_dir=tmp_path))
    ctx.edge = edge.Edge(ctx)
    secret = tmp_path / 'secrets/mobile-edge-key'
    secret.parent.mkdir()
    secret.write_text(KEY)
    secret.chmod(0o600)
    with write_db(db) as s:
        person = User(username='casey', password_hash='test-only', totp_secret='test-only')
        s.add(person)
        s.flush()
        s.add(Node(id=NODE, name='roamer', status='active', public_git_backup=True))
        for pid, host, backup in [(1, 'roamer', True), (2, 'roamer', False), (3, 'other', True), (4, 'roamer', True)]:
            s.add(Project(id=pid, slug=f'project-{pid}', name=f'Project {pid}', host=host, git_backup=backup,
                          workspace_path=f'/private/project-{pid}', account='agent'))
        for kind, fingerprint in [('node', MACHINE), ('native', NATIVE), ('browser', BROWSER)]:
            s.add(Device(id=kind, person='' if kind == 'node' else 'casey', kind=kind, label=kind,
                         node_id=NODE if kind == 'node' else None, status='active', certificate_pem='test metadata',
                         fingerprint=fingerprint, serial=kind, expires_at=auth.now() + timedelta(days=1),
                         installation_id='f' * 32 if kind == 'native' else None,
                         platform='android' if kind == 'native' else None, key_hash='a' * 64 if kind == 'native' else None))
        s.add(DevicePairing(id='pairing', person='casey', label='Phone', token_hash='test-token-hash', kind='native',
                            installation_id='f' * 32, platform='android', key_hash='a' * 64, csr_pem='test CSR',
                            renew_id='native', renew_fingerprint=NATIVE, expires_at=auth.now() + timedelta(days=1)))
        s.add(AuthSession(token_hash='test-session-hash', csrf='test-csrf', user_id=person.id,
                          device_fingerprint=NATIVE, device_bound_at=auth.now(), expires_at=auth.now() + timedelta(days=1)))
        s.add(Setting(key='remote_access', value={'machine_identities': True, 'public_hostname': 'remote.example.com'}))
        s.add(Setting(key='remote_access_runtime', value={'enabled': True, 'desired_enabled': True,
                                                        'pending': False, 'hostname': 'remote.example.com'}))
        s.commit()
    try:
        yield ctx, path
    finally:
        engine.dispose()


@pytest.mark.parametrize('mobile', [False, True])
@pytest.mark.parametrize('fingerprint', [MACHINE, NATIVE, BROWSER])
def test_mobile_admission_never_widens_git_scope(world, fingerprint, mobile):
    ctx, _ = world
    ctx.settings.mobile_enabled = mobile
    # Same-host backups only. Even a valid native identity and enabled mobile cannot use git routes.
    for pid, expected in [(1, fingerprint == MACHINE), (2, False), (3, False), (4, fingerprint == MACHINE), (99, False)]:
        for suffix in ('/token', '/info/refs', '/git-receive-pack', '/git-upload-pack'):
            assert bool(ctx.edge.check(fingerprint, f'/api/git/{pid}.git{suffix}')) == expected


async def test_sweep_checks_each_path_even_when_streams_share_a_machine_leaf(world):
    ctx, _ = world
    one = edge.Connection(MACHINE, GIT)
    four = edge.Connection(MACHINE, '/api/git/4.git/git-receive-pack')
    control = edge.Connection(MACHINE, '/api/nodes/connect')
    ctx.edge.open.update([one, four, control])
    assert await ctx.edge.sweep() == 0
    with write_db(ctx.db) as s:
        s.get(Project, 1).git_backup = False
        s.commit()
    assert await ctx.edge.sweep() == 1
    assert one.closed.is_set() and not four.closed.is_set() and not control.closed.is_set()
    ctx.edge.open.remove(one)
    with write_db(ctx.db) as s:
        s.get(Node, NODE).public_git_backup = False
        s.commit()
    assert await ctx.edge.sweep() == 1
    assert four.closed.is_set() and not control.closed.is_set()


@pytest.mark.parametrize('change', ['revoke', 'retire', 'remove-node', 'move-project', 'consent-off',
                                  'native-revoke', 'native-key-rotate', 'native-key-missing', 'mobile-off'])
async def test_actual_edge_streams_disconnect_on_revocation_and_key_invalidation(world, change):
    ctx, _ = world
    incoming = {'node': asyncio.Queue(), 'native': asyncio.Queue()}
    started = {kind: asyncio.Event() for kind in incoming}
    results = {}

    async def handler(scope, receive, send):
        kind = 'native' if scope['state']['edge_native'] else 'node'
        started[kind].set()
        results[kind] = await receive()

    async def send(message):
        pass

    async def run(kind, fingerprint, path):
        headers = [(edge.HEADER, fingerprint.encode()), (b'host', b'remote.example.com')]
        if kind == 'native':
            headers.append((b'x-acc-native-edge', KEY.encode()))
        await edge.EdgeGuard(handler, ctx)({'type': 'http', 'path': path, 'headers': headers, 'client': None},
                                           incoming[kind].get, send)

    tasks = {'node': asyncio.create_task(run('node', MACHINE, GIT)),
             'native': asyncio.create_task(run('native', NATIVE, '/api/projects'))}
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started.values())), 5)
        with write_db(ctx.db) as s:
            if change == 'revoke':
                s.get(Device, 'node').status = 'revoked'
            elif change == 'retire':
                s.get(Device, 'node').retire_after = auth.now() - timedelta(seconds=1)
            elif change == 'remove-node':
                s.get(Node, NODE).status = 'removed'
            elif change == 'move-project':
                s.get(Project, 1).host = 'other'
            elif change == 'consent-off':
                s.get(Node, NODE).public_git_backup = False
            elif change == 'native-revoke':
                s.get(Device, 'native').status = 'revoked'
            s.commit()
        secret = ctx.settings.data_dir / 'secrets/mobile-edge-key'
        if change == 'native-key-rotate':
            secret.write_text('f' * 64)
        elif change == 'native-key-missing':
            secret.unlink()
        elif change == 'mobile-off':
            ctx.settings.mobile_enabled = False
        assert await ctx.edge.sweep() == 1
        closed = 'native' if change.startswith('native-') or change == 'mobile-off' else 'node'
        await asyncio.wait_for(tasks[closed], 5)
        assert results[closed] == {'type': 'http.disconnect'}
        other = 'node' if closed == 'native' else 'native'
        assert other not in results  # one device's invalidation does not stop another identity's stream
    finally:
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
    assert not ctx.edge.open


def test_0068_round_trip_on_copy_preserves_populated_mobile_rows(world, tmp_path):
    ctx, source = world
    ctx.db.kw['bind'].dispose()
    command.downgrade(alembic_config(f'sqlite:///{source}'), '0067')
    copy = tmp_path / 'copy.db'
    with closing(sqlite3.connect(source)) as before, closing(sqlite3.connect(copy)) as after:
        before.backup(after)

    def snapshot(path):
        with closing(sqlite3.connect(path)) as db:
            assert db.execute('PRAGMA foreign_key_check').fetchall() == []
            assert db.execute('PRAGMA integrity_check').fetchone() == ('ok',)
            rows = {table: db.execute(f'SELECT * FROM {table} ORDER BY 1').fetchall()
                    for table in ('user', 'device', 'device_pairing', 'auth_session', 'project', 'setting')}
            columns = [column[1] for column in db.execute('PRAGMA table_info(node)') if column[1] != 'public_git_backup']
            rows['node'] = db.execute('SELECT ' + ','.join(columns) + ' FROM node ORDER BY id').fetchall()
            return rows

    expected = snapshot(source)
    config = alembic_config(f'sqlite:///{copy}')
    command.upgrade(config, '0068')
    assert snapshot(copy) == expected
    with closing(sqlite3.connect(copy)) as db:
        assert db.execute('SELECT public_git_backup FROM node').fetchall() == [(0,)]
        db.execute('UPDATE node SET public_git_backup=1')
        db.commit()
        with pytest.raises(sqlite3.IntegrityError, match='device.person, device.kind, device.installation_id'):
            db.execute("INSERT INTO device (id,person,label,kind,status,certificate_pem,fingerprint,serial,expires_at,"
                       "last_seen_at,created_at,node_id,retire_after,installation_id,platform,key_hash) SELECT 'duplicate',person,label,kind,status,certificate_pem,"
                       "'duplicate-fingerprint','duplicate-serial',expires_at,last_seen_at,created_at,node_id,retire_after,"
                       "installation_id,platform,key_hash FROM device WHERE kind='native'")
    command.downgrade(config, '0067')
    assert snapshot(copy) == expected
    with closing(sqlite3.connect(copy)) as db, closing(sqlite3.connect(source)) as original:
        assert 'public_git_backup' not in {column[1] for column in db.execute('PRAGMA table_info(node)')}
        assert original.execute('SELECT version_num FROM alembic_version').fetchone() == ('0067',)
        assert 'public_git_backup' not in {column[1] for column in original.execute('PRAGMA table_info(node)')}


@pytest.mark.parametrize('change,transport', [('node-revoke', 'http'), ('node-consent-off', 'http'),
                                              ('native-key-rotate', 'http'), ('native-key-rotate', 'websocket')])
async def test_completed_invalidation_wins_over_a_simultaneously_ready_body(world, change, transport):
    ctx, _ = world
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
    headers = [(edge.HEADER, (NATIVE if native else MACHINE).encode()), (b'host', b'remote.example.com')]
    if native:
        headers.append((b'x-acc-native-edge', KEY.encode()))
    task = asyncio.create_task(edge.EdgeGuard(handler, ctx)(
        {'type': transport, 'path': '/api/projects' if native else GIT, 'headers': headers, 'client': None}, receive, send))
    try:
        await asyncio.wait_for(waiting.wait(), 5)
        with write_db(ctx.db) as s:
            if change == 'node-revoke':
                s.get(Device, 'node').status = 'revoked'
            elif change == 'node-consent-off':
                s.get(Node, NODE).public_git_backup = False
            s.commit()
        if native:
            (ctx.settings.data_dir / 'secrets/mobile-edge-key').write_text('f' * 64)
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
