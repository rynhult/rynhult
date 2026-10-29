"""Machine identities: node-held keys, issuance and rotation over the signed link, removal, and the public
edge's per-request and established-connection enforcement inside accd."""
import asyncio
import base64
import json
import ssl
import time
from datetime import timedelta
from pathlib import Path

import pytest
import websockets
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from helpers import wait_for, write_db
from test_auth import live  # noqa: F401 (fixture)
from test_device_ca import helper as signer
from test_device_ca_machine import csr as make_csr
from test_multihost import enroll
from test_passkeys import setup

from acc.node import machine
from acc.node.identity import tls_context
from acc.server import auth, devices, edge
from acc.server.models import AuditLog, Device, Node, Project, Setting, User


@pytest.fixture(autouse=True)
def public_routing_on(monkeypatch):
    """These tests model requests arriving through an edge that is on; Off is test_remote_access_off_streams."""
    monkeypatch.setattr(edge, 'public_on', lambda s: True)


@pytest.fixture
def authority(tmp_path, monkeypatch):
    ca = signer()
    directory = tmp_path / 'authority'
    ca.initialize(directory)
    monkeypatch.setattr(devices, 'sign', lambda body: ca.issue(directory, body))
    return ca, directory


def machines_on(api, on=True):
    with write_db(api.app.state.db) as s:
        row = s.get(Setting, 'remote_access')
        s.merge(Setting(key='remote_access', value={**(row.value if row else {}), 'machine_identities': on}))
        s.commit()


# How long to wait for a message that will arrive (a node link's ready, a CSR request, a certificate). The gate's full
# parallel run once took longer than 5s here (orch0 gate, 2026-10-26); `silent` keeps its short wait on purpose.
ARRIVES = 15


def fingerprint(pem):
    return x509.load_pem_x509_certificate(pem.encode()).fingerprint(hashes.SHA256()).hex()


async def link(live, key, node_id, presented=None):  # noqa: F811
    """A node link as it arrives through the edge: Caddy sets the verified leaf's fingerprint header."""
    headers = {'X-Acc-Edge-Client': presented} if presented else {}
    ws = await websockets.connect(f'{live}/api/nodes/connect', additional_headers=headers)
    challenge = json.loads(await ws.recv())
    signature = key.sign(base64.b64decode(challenge['nonce']) + challenge['primary'].encode())
    await ws.send(json.dumps({'t': 'hello', 'node_id': node_id, 'signature': base64.b64encode(signature).decode(),
                              'info': {}, 'accounts': [], **({'machine': presented} if presented else {})}))
    assert json.loads(await asyncio.wait_for(ws.recv(), ARRIVES)) == {'t': 'ready'}
    return ws


async def next_text(ws, kind, timeout=ARRIVES):
    while True:
        message = await asyncio.wait_for(ws.recv(), timeout)
        if isinstance(message, str) and json.loads(message).get('t') == kind:
            return json.loads(message)


async def silent(ws, kinds, timeout=0.5):
    try:
        while True:
            message = await asyncio.wait_for(ws.recv(), timeout)
            assert not (isinstance(message, str) and json.loads(message).get('t') in kinds), message
    except asyncio.TimeoutError:
        return True


async def issue_over_link(ws, node_dir, node_id):
    await next_text(ws, 'machine-csr-request')
    await ws.send(json.dumps({'t': 'machine-csr', 'csr': machine.new_request(node_dir, node_id)}))
    certificate = (await next_text(ws, 'machine-certificate'))['certificate']
    assert machine.accept(node_dir, certificate)
    ready = await next_text(ws, 'machine-ready')
    assert ready['fingerprint'] == fingerprint(certificate)
    assert machine.switch(node_dir, ready['fingerprint'])
    return ready['fingerprint']


async def test_machine_identity_is_off_by_default(api, env, live, authority):  # noqa: F811
    key, node_id = await enroll(api)
    ws = await link(live, key, node_id)
    assert await silent(ws, {'machine-csr-request'})
    await ws.close()
    with api.app.state.db() as s:
        assert s.query(Device).count() == 0


@pytest.mark.parametrize('retirement_delay', [0, 0.6], ids=['immediate', 'slow-retirement'])
async def test_link_issues_rotates_and_retires_without_dropping_the_node(
        api, env, live, authority, tmp_path, monkeypatch, retirement_delay):  # noqa: F811
    machines_on(api)
    key, node_id = await enroll(api)
    node_dir = tmp_path / 'node'
    node_dir.mkdir(mode=0o700)
    ws = await link(live, key, node_id)
    first = await issue_over_link(ws, node_dir, node_id)
    await ws.close()
    with api.app.state.db() as s:
        row = s.scalar(edge.select(Device).where(Device.fingerprint == first))
        assert (row.kind, row.node_id, row.person, row.status) == ('node', node_id, '', 'active')
        assert row.expires_at - auth.now() <= timedelta(days=30)
    # Presenting a fresh leaf through the edge: no new request.
    ws = await link(live, key, node_id, presented=first)
    assert await silent(ws, {'machine-csr-request'})
    await ws.close()
    # A third of its life left: the node gets a successor while the current leaf keeps working.
    with write_db(api.app.state.db) as s:
        s.scalar(edge.select(Device).where(Device.fingerprint == first)).expires_at = auth.now() + timedelta(days=5)
        s.commit()
    ws = await link(live, key, node_id, presented=first)
    second = await issue_over_link(ws, node_dir, node_id)
    assert second != first
    with api.app.state.db() as s:
        old = s.scalar(edge.select(Device).where(Device.fingerprint == first))
        assert old.status == 'active' and old.retire_after - auth.now() <= timedelta(hours=24)
        assert set(api.app.state.ctx.remote_access.payload()['leaves']) >= {old.certificate_pem}
    # An unsolicited CSR (the successor is issued, not yet presented) never reaches the signer.
    calls = []
    sign = devices.sign
    devices.sign = lambda body: calls.append(body) or sign(body)
    try:
        await ws.close()
        again = await link(live, key, node_id, presented=first)
        await again.send(json.dumps({'t': 'machine-csr', 'csr': make_csr()[1]}))
        assert await silent(again, {'machine-certificate'})
        await again.close()
    finally:
        devices.sign = sign
    assert calls == []
    switched = edge.switched

    def delayed_switch(*args):
        time.sleep(retirement_delay)  # simulate a slow identity worker, not a synchronization wait
        return switched(*args)

    monkeypatch.setattr(edge, 'switched', delayed_switch)
    ws = await link(live, key, node_id, presented=second)
    # ready acknowledges the signed link, not the asynchronous identity follow-up.
    # Closing now cancels that follow-up; wait for its durable effect first.
    def retired():
        with api.app.state.db() as s:
            return s.scalar(edge.select(Device.status).where(Device.fingerprint == first)) == 'retired'

    await wait_for(retired)
    await ws.close()
    with api.app.state.db() as s:
        assert s.scalar(edge.select(Device.status).where(Device.fingerprint == first)) == 'retired'
        assert s.scalar(edge.select(Device.status).where(Device.fingerprint == second)) == 'active'
        assert [a.action for a in s.scalars(edge.select(AuditLog).where(AuditLog.action.like('machine_identity.%'))
                                            .order_by(AuditLog.id))] == ['machine_identity.issue'] * 2 + ['machine_identity.rotate']
    # The retired leaf is refused per request; the current one is admitted.
    assert (await api.get('/api/nodes/bundle', headers={'X-Acc-Edge-Client': first})).status_code == 403
    assert (await api.get('/api/nodes/bundle', headers={'X-Acc-Edge-Client': second})).status_code != 403


async def test_identity_is_bound_to_its_node(api, env, live, authority, tmp_path):  # noqa: F811
    machines_on(api)
    key, node_id = await enroll(api)
    other_key, other_id = await enroll(api, name='laptop')
    node_dir = tmp_path / 'node'
    node_dir.mkdir(mode=0o700)
    ws = await link(live, key, node_id)
    presented = await issue_over_link(ws, node_dir, node_id)
    await ws.close()
    with pytest.raises(websockets.ConnectionClosed) as closed:
        await link(live, other_key, other_id, presented=presented)
    assert closed.value.rcvd.code == 4401


async def test_removing_a_machine_revokes_its_identity_and_closes_its_public_link(api, env, live, authority, tmp_path):  # noqa: F811
    machines_on(api)
    key, node_id = await enroll(api)
    node_dir = tmp_path / 'node'
    node_dir.mkdir(mode=0o700)
    ws = await link(live, key, node_id)
    presented = await issue_over_link(ws, node_dir, node_id)
    await ws.close()
    public = await link(live, key, node_id, presented=presented)
    removed = await api.post('/api/nodes/pc/remove', json={})
    assert removed.status_code == 200
    with pytest.raises(websockets.ConnectionClosed):
        await asyncio.wait_for(public.recv(), 5)
    with api.app.state.db() as s:
        assert s.scalar(edge.select(Device.status).where(Device.fingerprint == presented)) == 'revoked'
        assert presented not in [fingerprint(p) for p in api.app.state.ctx.remote_access.payload()['leaves']]
    assert (await api.get('/api/nodes/bundle', headers={'X-Acc-Edge-Client': presented})).status_code == 403


async def test_expiry_or_setting_off_closes_an_established_link_at_the_sweep(api, env, live, authority, tmp_path):  # noqa: F811
    machines_on(api)
    key, node_id = await enroll(api)
    node_dir = tmp_path / 'node'
    node_dir.mkdir(mode=0o700)
    ws = await link(live, key, node_id)
    presented = await issue_over_link(ws, node_dir, node_id)
    await ws.close()
    public = await link(live, key, node_id, presented=presented)
    machines_on(api, False)
    assert await api.app.state.ctx.remote_access.sweep() is None
    with pytest.raises(websockets.ConnectionClosed) as closed:
        await asyncio.wait_for(public.recv(), 5)
    assert closed.value.rcvd.code == 4403
    assert presented not in [fingerprint(p) for p in api.app.state.ctx.remote_access.payload()['leaves']]


def register(s, kind, fingerprint_, **fields):
    s.add(Device(id=fingerprint_[:32], label='Example', kind=kind, status='active', certificate_pem='-',
                 fingerprint=fingerprint_, serial=fingerprint_[:20], expires_at=auth.now() + timedelta(days=1), **fields))


async def test_edge_requests_are_split_by_identity_kind(api, env, project, monkeypatch):
    headers, _ = await setup(api, env, monkeypatch)
    machines_on(api)
    browser, node, stranger = 'b' * 64, 'c' * 64, 'd' * 64
    with write_db(api.app.state.db) as s:
        person = s.query(User).filter_by(role='owner').first().username
        s.add(Node(id='e' * 32, name='pc', status='active', public_git_backup=True))
        register(s, 'browser', browser, person=person)
        register(s, 'node', node, person='', node_id='e' * 32)
        s.get(Project, project['id']).host = 'pc'
        s.commit()

    async def status(path, fp, method='GET'):
        return (await api.request(method, path, headers={'X-Acc-Edge-Client': fp})).status_code

    assert await status('/api/devices', browser) == 200
    # A machine identity is not a person's device: never listed or revocable as one.
    assert (await api.get('/api/devices')).json()['devices'] == [
        d for d in (await api.get('/api/devices')).json()['devices'] if d['kind'] == 'browser']
    assert len((await api.get('/api/devices')).json()['devices']) == 1
    assert (await api.post(f'/api/devices/{node[:32]}/revoke', headers=headers, json={})).status_code == 404
    for path in ('/api/nodes/bundle', '/api/nodes/install.sh', '/api/nodes/enroll', '/api/device-pairing/claim',
                 f'/api/git/{project["id"]}.git/info/refs'):
        assert await status(path, browser) == 403, path
    assert await status('/api/nodes/bundle', node) != 403
    git = f'/api/git/{project["id"]}.git/info/refs?service=git-receive-pack'
    # Admitted: what the git route itself answers without a push token, exactly as on the private address.
    admitted = await api.get(git, headers={'X-Acc-Edge-Client': node})
    assert admitted.status_code == (await api.get(git)).status_code
    assert 'identity is not allowed' not in admitted.text
    assert await status(f'/api/git/{project["id"] + 1}.git/info/refs', node) == 403  # not a project on this machine
    for path in ('/api/devices', '/api/nodes/install.sh', '/api/nodes/enroll', '/api/nodes/ca', '/api/projects'):
        assert await status(path, node) == 403, path
    assert await status('/api/devices', stranger) == 403
    assert await status('/api/devices', 'not-hex') == 403
    two = await api.get('/api/devices', headers=[('X-Acc-Edge-Client', browser), ('X-Acc-Edge-Client', browser)])
    assert two.status_code == 403
    # Without the header (private access) nothing changes.
    assert (await api.get('/api/nodes/install.sh')).status_code == 200


class FakeEdge:
    def __init__(self):
        self.valid = True
        self.open = set()

    def check(self, fingerprint, path):
        return {'kind': 'browser', 'public_hostname': ''} if self.valid else None

    async def sweep(self):
        for connection in list(self.open):
            if not self.valid:
                connection.closed.set()


async def guarded(app, scope_type):
    ctx = type('Ctx', (), {})()
    ctx.edge = FakeEdge()
    guard = edge.EdgeGuard(app, ctx)
    inbox, sent = asyncio.Queue(), []

    async def send(message):
        sent.append(message)
    scope = {'type': scope_type, 'path': '/x', 'headers': [(b'x-acc-edge-client', b'a' * 64)]}
    task = asyncio.create_task(guard(scope, inbox.get, send))
    return ctx, inbox, sent, task


async def test_open_websocket_and_stream_end_when_the_identity_does():
    async def websocket_app(scope, receive, send):
        assert (await receive())['type'] == 'websocket.connect'
        await send({'type': 'websocket.accept'})
        while (await receive())['type'] != 'websocket.disconnect':
            await send({'type': 'websocket.send', 'text': 'echo'})

    ctx, inbox, sent, task = await guarded(websocket_app, 'websocket')
    await inbox.put({'type': 'websocket.connect'})
    await inbox.put({'type': 'websocket.receive', 'text': 'hi'})
    await wait_for(lambda: len(sent) == 2)
    assert [m['type'] for m in sent] == ['websocket.accept', 'websocket.send']
    assert len(ctx.edge.open) == 1
    ctx.edge.valid = False
    await ctx.edge.sweep()
    await asyncio.wait_for(task, 2)  # the handler ended although the peer sent nothing more
    assert sent[-1] == {'type': 'websocket.close', 'code': 4403}
    assert ctx.edge.open == set()

    async def stream_app(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        while True:  # a long poll / event stream waiting on the client
            if (await receive())['type'] == 'http.disconnect':
                return

    ctx, inbox, sent, task = await guarded(stream_app, 'http')
    await wait_for(lambda: sent and len(ctx.edge.open) == 1)
    ctx.edge.valid = False
    await ctx.edge.sweep()
    await asyncio.wait_for(task, 2)
    ctx, inbox, sent, task = await guarded(stream_app, 'http')
    ctx.edge.valid = False
    await asyncio.wait_for(task, 2)
    assert sent[0]['status'] == 403  # refused before the application runs


def test_sweep_and_reconciliation_run_well_inside_the_promised_minute():
    import inspect

    from acc.server import remote_access
    assert remote_access.SWEEP_SECONDS == 15
    assert 'await self.sweep()' in inspect.getsource(remote_access.RemoteAccess.sweeping)
    assert 'asyncio.sleep(15)' in inspect.getsource(remote_access.RemoteAccess.run)


async def test_node_presents_its_rotated_identity_and_keeps_its_server_pin(tmp_path, authority):
    from test_node import CA
    ca, directory = authority
    node_dir = tmp_path / 'node'
    node_dir.mkdir(mode=0o700)
    request = machine.new_request(node_dir, 'f' * 32)
    issued = ca.issue(directory, {'operation': 'issue_machine', 'node_id': 'f' * 32, 'csr': request, 'days': 30})
    assert not machine.accept(node_dir, ca.issue(directory, {'operation': 'issue_machine', 'node_id': 'f' * 32,
                                                             'csr': make_csr()[1], 'days': 30})['certificate'])
    assert machine.accept(node_dir, issued['certificate'])
    assert machine.current(node_dir) is None  # not used until the main says the edge admits it
    assert not machine.switch(node_dir, '0' * 64)
    assert machine.switch(node_dir, fingerprint(issued['certificate']))
    assert machine.fingerprint(node_dir) == fingerprint(issued['certificate'])
    assert (node_dir / machine.IDENTITY).stat().st_mode & 0o777 == 0o600
    # A TLS server that asks for client certificates sees the machine leaf; server trust stays the pinned CA.
    server_ca = CA(tmp_path / 'server')
    server = server_ca.server_context()
    server.verify_mode = ssl.CERT_REQUIRED
    server.load_verify_locations(cadata=(directory / 'ca.pem').read_text())
    seen = []

    async def handle(reader, writer):
        seen.append(writer.get_extra_info('ssl_object').getpeercert(binary_form=True))
        writer.close()
    listener = await asyncio.start_server(handle, '127.0.0.1', 0, ssl=server)
    port = listener.sockets[0].getsockname()[1]
    context = tls_context('ca', server_ca.path, identity=machine.current(node_dir))
    _, writer = await asyncio.open_connection('127.0.0.1', port, ssl=context, server_hostname='localhost')
    await wait_for(lambda: seen)
    writer.close()
    await writer.wait_closed()
    assert x509.load_der_x509_certificate(seen[0]).fingerprint(hashes.SHA256()).hex() == fingerprint(issued['certificate'])
    with pytest.raises(ssl.SSLCertVerificationError):  # another server CA is still refused
        await asyncio.open_connection('127.0.0.1', port, server_hostname='localhost',
                                      ssl=tls_context('ca', ca_pem=CA(tmp_path / 'other').pem.decode(),
                                                      identity=machine.current(node_dir)))
    listener.close()
    await listener.wait_closed()
    # A loose identity file is never presented.
    (node_dir / machine.IDENTITY).chmod(0o644)
    assert machine.current(node_dir) is None


# Security review fixes (docs/reviews/2026-10-24-review-remote-access-machines.md).

async def test_a_revocation_during_the_admission_check_still_ends_the_stream():
    """The connection is registered before the registry check, so a revocation's immediate sweep that runs while the
    check is in flight (and read the row before the revocation committed) still reaches it."""
    import threading
    checking, release = threading.Event(), threading.Event()

    class RacingEdge(FakeEdge):
        def check(self, fingerprint, path):
            checking.set()
            release.wait(2)
            return {'kind': 'browser', 'public_hostname': ''}  # read before the revocation committed

    async def websocket_app(scope, receive, send):
        assert (await receive())['type'] == 'websocket.connect'
        await send({'type': 'websocket.accept'})
        while (await receive())['type'] != 'websocket.disconnect':
            pass

    ctx = type('Ctx', (), {})()
    ctx.edge = RacingEdge()
    sent = []

    async def send(message):
        sent.append(message)
    inbox = asyncio.Queue()
    scope = {'type': 'websocket', 'path': '/x', 'headers': [(b'x-acc-edge-client', b'a' * 64)]}
    task = asyncio.create_task(edge.EdgeGuard(websocket_app, ctx)(scope, inbox.get, send))
    assert await asyncio.to_thread(checking.wait, 2)
    ctx.edge.valid = False
    await ctx.edge.sweep()  # the revoke route's immediate sweep
    release.set()
    await inbox.put({'type': 'websocket.connect'})
    await asyncio.wait_for(task, 2)
    assert sent[-1] == {'type': 'websocket.close', 'code': 4403}
    assert ctx.edge.open == set()


async def test_the_cut_off_sweep_does_not_wait_for_a_slow_reconciliation(monkeypatch):
    """A reconciliation can hold the lock for minutes (helper timeouts, an owner's apply); expiry, retirement and the
    setting going off must still end streams on the sweep's own schedule."""
    from acc.server import remote_access
    monkeypatch.setattr(remote_access, 'SWEEP_SECONDS', 0.01, raising=False)
    monkeypatch.setattr(remote_access.asyncio, 'sleep', _fast_sleep)
    swept = []
    retried = asyncio.Event()
    runner = remote_access.RemoteAccess.__new__(remote_access.RemoteAccess)

    async def hang():
        await asyncio.Event().wait()

    async def sweep():
        swept.append(1)
        if len(swept) == 1:
            raise RuntimeError('database busy')  # one failed sweep must not stop later ones
        if len(swept) >= 3:
            retried.set()
    runner.reconcile, runner.sweep = hang, sweep
    task = asyncio.create_task(runner.run())
    try:
        await asyncio.wait_for(retried.wait(), 2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert len(swept) >= 3


_real_sleep = asyncio.sleep


async def _fast_sleep(seconds):
    await _real_sleep(min(seconds, 0.01))


async def test_closing_terminals_survives_a_stream_the_edge_already_ended():
    """The edge guard makes later sends raise OSError. Closing terminals on revoke or removal must keep going (the
    person's other terminals, the immediate sweep, reconciliation and session stops come after it)."""
    from acc.server.api.context import Context

    class Socket:
        def __init__(self, fails):
            self.fails, self.closed = fails, False

        async def send_text(self, text):
            if self.fails:
                raise OSError('edge identity is no longer valid')

        async def close(self, code=1000):
            if self.fails:
                raise OSError('edge identity is no longer valid')
            self.closed = True
    ended, private = Socket(True), Socket(False)
    holder = type('Holder', (), {})()
    holder.terminals = {'k': [ended, private]}
    await Context.close_terminals(holder, 'k')
    assert private.closed and 'k' not in holder.terminals


def test_root_update_helper_presents_a_private_copy_of_the_identity(tmp_path, authority):
    """The node account owns its state folder: root must not re-open machine.pem by name after checking it (a swapped
    link would be followed), so it loads the checked bytes from its own private copy."""
    ca, directory = authority
    node_dir = tmp_path / 'node'
    node_dir.mkdir(mode=0o700)
    issued = ca.issue(directory, {'operation': 'issue_machine', 'node_id': 'e' * 32,
                                  'csr': machine.new_request(node_dir, 'e' * 32), 'days': 30})
    assert machine.accept(node_dir, issued['certificate'])
    assert machine.switch(node_dir, fingerprint(issued['certificate']))
    private = tmp_path / 'stage'
    private.mkdir(mode=0o700)
    loaded = []

    class Context:
        def load_cert_chain(self, path):
            loaded.append((path, Path(path).read_bytes(), Path(path).stat().st_mode & 0o777))
    assert machine.present(Context(), node_dir, private) is True
    path, data, mode = loaded[0]
    assert Path(path).parent == private and mode == 0o600
    assert data == (node_dir / machine.IDENTITY).read_bytes()
    assert not Path(path).exists()  # the copy is gone once loaded
    (node_dir / machine.IDENTITY).chmod(0o644)
    assert machine.present(Context(), node_dir, private) is False and len(loaded) == 1


async def test_break_it_6_machine_registry_reserves_writer_after_signing(api, authority, monkeypatch):
    import sqlite3

    from helpers import pause_supervisor
    from sqlalchemy import event

    await pause_supervisor(api)
    machines_on(api)
    _, node_id = await enroll(api)
    engine = api.app.state.db.kw['bind']
    signed, attempted = False, []
    sign = devices.sign
    def after_sign(body):
        nonlocal signed
        result = sign(body)
        if body.get('operation') == 'issue_machine':
            signed = True  # authority queries do not sign a leaf or begin registry persistence
        return result
    monkeypatch.setattr(devices, 'sign', after_sign)
    def competing_commit(connection, cursor, statement, parameters, context, many):
        if not signed or attempted or 'FROM node' not in statement:
            return
        # Independent process/connection, outside the in-process transaction gate.
        # WAL permits this commit after a deferred reader, invalidating its snapshot.
        with sqlite3.connect(engine.url.database, timeout=.02) as other:
            try:
                other.execute("INSERT INTO audit_log(ts, actor, action, detail) VALUES (?, ?, ?, ?)",
                              ('2026-10-25 12:00:00', 'owner', 'fixture.concurrent', '{}'))
                other.commit()
                attempted.append('committed')
            except sqlite3.OperationalError as error:
                assert 'locked' in str(error)
                attempted.append('reserved')
    event.listen(engine, 'after_cursor_execute', competing_commit)
    try:
        certificate = await asyncio.to_thread(edge.issue, api.app.state.ctx, node_id, make_csr()[1], None)
    finally:
        event.remove(engine, 'after_cursor_execute', competing_commit)
    assert signed and attempted == ['reserved']
    with api.app.state.db() as db:
        assert db.scalar(edge.select(Device).where(Device.node_id == node_id)).certificate_pem == certificate
        assert db.scalar(edge.select(AuditLog).where(AuditLog.action == 'machine_identity.issue')) is not None


async def test_break_it_6_machine_switch_reserves_writer_before_read(api, authority):
    import sqlite3

    from helpers import pause_supervisor
    from sqlalchemy import event

    await pause_supervisor(api)
    machines_on(api)
    _, node_id = await enroll(api)
    pem = await asyncio.to_thread(edge.issue, api.app.state.ctx, node_id, make_csr()[1], None)
    engine, attempted = api.app.state.db.kw['bind'], []
    def competing_commit(connection, cursor, statement, parameters, context, many):
        if attempted or 'FROM device' not in statement:
            return
        with sqlite3.connect(engine.url.database, timeout=.02) as other:
            try:
                other.execute("INSERT INTO audit_log(ts, actor, action, detail) VALUES (?, ?, ?, ?)",
                              ('2026-10-25 12:00:00', 'owner', 'fixture.concurrent', '{}'))
                other.commit()
                attempted.append('committed')
            except sqlite3.OperationalError as error:
                assert 'locked' in str(error)
                attempted.append('reserved')
    event.listen(engine, 'after_cursor_execute', competing_commit)
    try:
        assert await asyncio.to_thread(edge.switched, api.app.state.ctx, node_id, fingerprint(pem)) is False
    finally:
        event.remove(engine, 'after_cursor_execute', competing_commit)
    assert attempted == ['reserved']
    with api.app.state.db() as db:
        assert db.scalar(edge.select(Device).where(Device.node_id == node_id)).last_seen_at is not None


@pytest.mark.parametrize('operation', ['issue', 'switched'])
async def test_identity_worker_does_not_invalidate_an_inflight_database_snapshot(api, authority, operation):
    """An owner transaction can still commit while issuance/retirement waits for the database."""
    import threading

    machines_on(api)
    _, node_id = await enroll(api)
    ctx = api.app.state.ctx
    request = make_csr()[1]
    presented = None
    if operation == 'switched':
        presented = fingerprint(await asyncio.to_thread(edge.issue, ctx, node_id, request, None))
    started, done = threading.Event(), threading.Event()

    def work():
        started.set()
        try:
            if operation == 'issue':
                return edge.issue(ctx, node_id, request, None)
            return edge.switched(ctx, node_id, presented)
        finally:
            done.set()

    task = None
    with api.app.state.db() as s:
        node = s.get(Node, node_id)  # deferred owner snapshot, before the background writer
        task = asyncio.create_task(asyncio.to_thread(work))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            # Give the competing worker an opportunity to commit; its reservation must wait for this reader.
            await asyncio.to_thread(done.wait, 0.2)
            node.info = {'owner_change': True}
            s.commit()  # previously SQLITE_BUSY_SNAPSHOT after the worker committed
        finally:
            s.close()
            await asyncio.wait_for(task, 5)
    with api.app.state.db() as s:
        assert s.get(Node, node_id).info == {'owner_change': True}
        leaves = edge.leaves_of(s, node_id)
        assert len(leaves) == 1
        if operation == 'switched':
            assert leaves[0].last_seen_at is not None


async def test_machines_list_says_whether_each_machine_has_a_public_identity(api, env, monkeypatch):
    # The Machines page shows this per machine: off, waiting for the next connection, valid until, or switching.
    await setup(api, env, monkeypatch)

    async def identity():
        rows = (await api.get('/api/nodes')).json()
        return next(r for r in rows if r['id'] == 'e' * 32)['machine_identity']
    with write_db(api.app.state.db) as s:
        s.add(Node(id='e' * 32, name='pc', status='active'))
        s.commit()
    assert await identity() == {'enabled': False, 'issued': False}
    machines_on(api)
    assert await identity() == {'enabled': True, 'issued': False}
    with write_db(api.app.state.db) as s:
        register(s, 'node', 'c' * 64, person='', node_id='e' * 32)
        s.commit()
    first = await identity()
    assert first['enabled'] and first['issued'] and first['fingerprint'] == 'c' * 64 and not first['rotating']
    assert first['last_presented_at'] is None and first['expires_at']
    with write_db(api.app.state.db) as s:
        register(s, 'node', 'd' * 64, person='', node_id='e' * 32)
        s.commit()
    assert (await identity())['rotating'] is True
    machines_on(api, False)  # its leaves stay recorded but are no longer admitted
    assert (await identity())['enabled'] is False
