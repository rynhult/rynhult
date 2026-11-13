"""Offline lab: real enrolled node wire, main/broker and exact native owner.

Invoked only by the fixed parent probe in an empty bubblewrap namespace.
No host service, host account, real login, model call or writable capability.
"""
from __future__ import annotations

import asyncio
import base64
import errno
import ipaddress
import json
import os
import pwd
import socket
import ssl
import sys
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi import FastAPI
from relay_helpers import setup
from websockets.asyncio.client import connect, unix_connect
from websockets.exceptions import ConnectionClosed

from acc.broker.config import BrokerConfig
from acc.broker.relay_runtime import CLI_DIGEST, RuntimeTransportFactory
from acc.broker.server import Broker
from acc.node.client import AuthRejected
from acc.node.client import Node as NodeClient
from acc.node.config import NodeConfig
from acc.server.api.nodes import router
from acc.server.broker_client import Brokers
from acc.server.db import write_intent
from acc.server.ha import Authority, Fenced
from acc.server.ha_runtime import Runtime as HARuntime
from acc.server.models import Node, Project, RelayOperation, RelayRun, RelaySession, Session, SessionRun
from acc.server.nodes import NodeHub
from acc.server.session_relay.hub import Hub
from acc.server.session_relay.runtime import RuntimeManager
from acc.session_relay import protocol as p
from acc.session_relay.runtime_peer import RuntimePeer

SID = 'abc123def456'


async def until(check, timeout=10):
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(.01)


class Partition:
    """A real TCP forwarding hop. A cut discards bytes in both directions."""
    def __init__(self, port):
        self.port = port
        self.cut = False
        self.tasks = set()
        self.writers = set()
        self.discarded = 0

    async def accept(self, reader, writer):
        handler = asyncio.current_task()
        self.tasks.add(handler)
        peer_reader, peer_writer = await asyncio.open_connection('127.0.0.1', self.port)
        self.writers.update((writer, peer_writer))
        async def pump(source, target):
            while data := await source.read(65536):
                if self.cut:
                    self.discarded += len(data)
                else:
                    target.write(data)
                    await target.drain()
        pumps = [asyncio.create_task(pump(reader, peer_writer)), asyncio.create_task(pump(peer_reader, writer))]
        try:
            done, _ = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            for ended in done:
                await ended
        finally:
            for task in pumps:
                task.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            for stream in (writer, peer_writer):
                stream.close()
                try:
                    await stream.wait_closed()
                except (OSError, ConnectionError):
                    pass
                self.writers.discard(stream)
            self.tasks.discard(handler)

    async def close(self):
        for stream in tuple(self.writers):
            stream.close()
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def require_refused_read(reader, native):
    # Retirement can close the native view before send(), or while receiving.
    # Both are refusal; a successful result is never accepted as one.
    try:
        await reader.send(json.dumps({'id':3,'method':'thread/read','params':{'threadId':native}}))
        assert 'result' not in json.loads(await asyncio.wait_for(reader.recv(), 40))
    except ConnectionClosed:
        pass


def phase(value):
    Path('/evidence/phase.txt').write_text(value)
    print('Offline partition phase: ' + value, file=sys.stderr, flush=True)


async def main():
    assert not Path('/srv/acc').exists()
    assert {name for _, name in socket.if_nameindex()} == {'lo'}
    with socket.socket() as check:
        assert check.connect_ex(('192.0.2.1', 443)) == errno.ENETUNREACH
    assert not os.environ.get('ANTHROPIC_API_KEY')
    root = Path('/tmp/enrolled')
    root.mkdir(mode=0o700)
    c = setup(root)
    account = pwd.getpwuid(os.getuid()).pw_name
    work = root / 'work'
    work.mkdir()
    sockets = root / 'sockets'
    sockets.mkdir(mode=0o700)
    run_dir = root / 'runs'
    run_dir.mkdir(mode=0o700)
    executable = Path('/usr/bin/codex').resolve()
    if executable.suffix == '.js':
        executable, = executable.parent.parent.glob('node_modules/@openai/codex-*/vendor/*/bin/codex')
    assert p.sha256_digest(executable.read_bytes()) == CLI_DIGEST
    config = BrokerConfig(socket_path=sockets / (account + '.sock'), run_dir=run_dir,
        allowed_roots=(work,), allowed_uids=frozenset({os.getuid()}),
        providers={'codex': str(executable)}, hook_path='/usr/bin/true')
    factory = RuntimeTransportFactory(config, owned_view_factory=None)
    broker = Broker(config, relay_transport=factory)
    broker_server = await asyncio.start_unix_server(broker.handle, path=str(config.socket_path), limit=1 << 20)
    hub = NodeHub()
    leader = Authority(c.engine, 'fixture-main')
    leader.promote(expected_epoch=0)
    brokers = Brokers(config.socket_path, sockets, account, 'fixture-services', hub=hub, authority=leader)
    c.ctx.broker = brokers
    delivery = SimpleNamespace(env={}, catalog=None, check_env=lambda _: None)
    c.ctx.supervisor = SimpleNamespace(catalog=None, _host_backoff={}, _wake=asyncio.Event(),
        tools=SimpleNamespace(tasks=set()), _backup_task=None,
        vault=SimpleNamespace(deliver=lambda *a, **k: delivery, redactor=lambda *a: SimpleNamespace(text=lambda s:s)))
    c.ctx.publisher = SimpleNamespace(tasks=set())
    c.ctx.principal = c.ctx.owner = lambda: c.resolve()[0]
    c.ctx.client_key = lambda _: 'offline-loopback'
    c.ctx.backup_jobs = SimpleNamespace(wake=asyncio.Event())
    c.ctx.updates = SimpleNamespace(follow=lambda _: None, follow_enabled=lambda _: False, build='fixture')
    c.ctx.edge = SimpleNamespace(step=lambda *a: (None, None))
    c.ctx.settings = replace(c.ctx.settings, allowed_origins=('http://fixture',), hook_path='/usr/bin/true')
    app = FastAPI()
    app.include_router(router(c.ctx))
    # The node uses its production TLS/handshake/connect/serve path. Its
    # disposable CA and key exist only in this offline mount/network namespace.
    tls_key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, 'Offline relay lab')])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
        .public_key(tls_key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
        .sign(tls_key, hashes.SHA256()))
    ca_file, key_file = root / 'ca.pem', root / 'tls.key'
    ca_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(tls_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                             serialization.NoEncryption()))
    key_file.chmod(0o600)
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', lifespan='off',
        ssl_keyfile=str(key_file), ssl_certfile=str(ca_file)))
    server_task = asyncio.create_task(server.serve(sockets=[listener]))
    await until(lambda: server.started)
    network = Partition(listener.getsockname()[1])
    hop = await asyncio.start_server(network.accept, '127.0.0.1', 0)
    primary = 'https://127.0.0.1:' + str(hop.sockets[0].getsockname()[1])
    url = primary.replace('https:', 'wss:') + '/api/nodes/connect'
    tls_context = ssl.create_default_context(cafile=str(ca_file))
    ws = relay = node_task = manager = ha_lifetime = admission = None
    connections = []
    mode = json.loads(Path('/evidence/mode.json').read_text())
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://fixture') as api:
            added = await api.post('/api/nodes', json={'name': 'lab-node'})
            assert added.status_code == 201, added.text
            key = Ed25519PrivateKey.generate()
            public = base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
            body = {'name':'lab-node', 'code':added.json()['code'], 'public_key':public, 'info':{'os':'linux'}}
            enrolled = await api.post('/api/nodes/enroll', json=body)
            assert enrolled.status_code == 200, enrolled.text
            assert (await api.post('/api/nodes/enroll', json=body)).status_code == 403
            node_id = enrolled.json()['node_id']
        config_node = NodeConfig(primary=primary, node_id=node_id, name='lab-node',
                                 trust='ca', ca_file=ca_file, socket_dir=sockets)
        async def open_node():
            previous = hub.links.get('lab-node')
            client = NodeClient(config_node, key, packages=None, request_replies_root=root / 'unprovisioned-replies')
            task = asyncio.create_task(client.connect_once())
            connections.append((client, task))
            await until(lambda: (task.done() or
                (hub.links.get('lab-node') is not previous and client.relay is not None)))
            if task.done():
                await task
                raise AssertionError('Node exited before ready')
            return client, client.relay, task
        wrong_key = ec.generate_private_key(ec.SECP256R1())
        wrong_ca = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(wrong_key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(wrong_key, hashes.SHA256()))
        wrong_ca_file = root / 'wrong-ca.pem'
        wrong_ca_file.write_bytes(wrong_ca.public_bytes(serialization.Encoding.PEM))
        bad_node = NodeClient(replace(config_node, ca_file=wrong_ca_file), key, packages=None)
        try:
            async with asyncio.timeout(5):
                await bad_node.connect_once()
        except ssl.SSLCertVerificationError:
            pass
        else:
            raise AssertionError('A different CA authenticated the node endpoint')
        assert not factory.active
        ws, relay, node_task = await open_node()
        link = hub.get('lab-node')
        assert 'session-runtime-v1' in link.info['capabilities']
        @write_intent
        def reset():
            with c.db() as db:
                db.delete(db.get(RelayRun, 1))
                db.delete(db.get(RelaySession, 'relay'))
                db.delete(db.get(SessionRun, 1))
                db.flush()
                session = db.get(Session, 'relay')
                session.id, session.run_as = SID, account
                session.project = Project(slug='offline-lab', name='Offline lab', host='lab-node',
                    workspace_path=str(work), trusted=True, owner='alex', account=account)
                session.run_dir, session.workdir = str(run_dir / SID), str(work)
                session.launch_spec.files = {'guardrails.json':'{}'}
                session.launch_spec.inputs = {'provider':'codex'}
                db.commit()
        reset()
        manager = RuntimeManager(c.ctx)
        c.ctx.supervisor.relay_manager = manager
        ha_lifetime = HARuntime(leader, c.ctx).lifespan()
        await ha_lifetime.__aenter__()
        launch = manager.new_launch(c.resolve)
        @write_intent
        def reserve():
            with c.db() as db:
                launch.reserve(db, db.get(Session, SID))
                db.commit()
        reserve()
        await launch.launch(SID)
        phase('native enrolled')
        state, = factory.active.values()
        native = state.binding.native_conversation
        assert state.owner.alive and state.native_socket.attested is False
        with c.db() as db:
            row = db.get(RelaySession, SID)
            assert row.capabilities == {}
            stored = db.get(RelayRun, row.current_run_id)
            assert stored.identity['placement'][2] == [node_id, public, id(link)]
            assert stored.identity['placement'][3:5] == [leader.epoch, leader.incarnation]
            assert stored.identity['binding']['account'] == account
        async with unix_connect(str(state.native_socket.path), compression=None) as reader:
            await reader.send(json.dumps({'id':1,'method':'initialize','params':{'clientInfo':{'name':'codex-tui','version':'0.167.1'}}}))
            assert 'result' in json.loads(await reader.recv())
            await reader.send(json.dumps({'id':2,'method':'thread/read','params':{'threadId':native}}))
            assert json.loads(await reader.recv())['result']['thread']['id'] == native
            # Existing enrollment must bind the currently reported account,
            # enrolled public key and private socket inode, not just node name.
            link.accounts.remove(account)
            assert await state.native_authorize() is False
            with c.db() as db:
                try:
                    launch.check_run(db)
                except p.RelayError:
                    pass
                else:
                    raise AssertionError('Withdrawn account retained authority')
            link.accounts.append(account)
            @write_intent
            def change_key(value):
                with c.db() as db:
                    db.get(Node, node_id).public_key = value
                    db.commit()
            alternate = Ed25519PrivateKey.generate()
            alternate_public = base64.b64encode(alternate.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
            change_key(alternate_public)
            assert await state.native_authorize() is False
            rejected = await connect(url, ssl=tls_context, proxy=None, ping_interval=None)
            try:
                try:
                    await ws.handshake(rejected)
                except AuthRejected:
                    pass
                else:
                    raise AssertionError('Old enrolled key authenticated a fresh link')
            finally:
                await rejected.close()
            with c.db() as db:
                try:
                    launch.check_run(db)
                except p.RelayError:
                    pass
                else:
                    raise AssertionError('Different enrolled key retained authority')
            change_key(public)
            phase('account and key refusals checked')
            pinned_socket = state.native_socket.path
            saved_socket = pinned_socket.with_name('pinned-native.sock')
            pinned_socket.rename(saved_socket)
            try:
                with socket.socket(socket.AF_UNIX) as substitute:
                    substitute.bind(str(pinned_socket))
                    pinned_socket.chmod(0o600)
                    try:
                        state.native_socket.validate()
                    except p.RelayError:
                        pass
                    else:
                        raise AssertionError('Same-owner socket replacement retained authority')
            finally:
                pinned_socket.unlink(missing_ok=True)
                saved_socket.rename(pinned_socket)
            state.native_socket.validate()
            assert state.native_socket.path.stat().st_mode & 0o777 == 0o600
            assert state.native_socket.directory.stat().st_mode & 0o777 == 0o700
            params = {'sessionId': SID, 'expectedRunId': str(launch.run_id),
                'text': 'Never delivered offline admission tripwire', 'expectedConfigurationRevision': 0,
                'expectedControlRevision': 1, 'ifBusy': 'reject'}
            digest = p.operation_digest('turn.submit', params)
            main_hub = Hub(c.ctx)
            registered = main_hub.register(c.resolve, SID, str(launch.run_id), 'partition-tripwire', 'turn.submit', digest)
            token = launch.issue(c.resolve, registered.operation_id, params)
            main_hub.queue(c.resolve, SID, registered.operation_id, expected_run=str(launch.run_id), digest=digest)
            main_hub.claim(c.resolve, SID, registered.operation_id)
            operation = launch.grants[token].operation
            await launch.peer.call('stage', {'grant': token})
            state.grant = token
            assert await state.verify(operation) is True
            before = state.broker.link
            # Refreshing only the broker journal cannot extend main authority.
            await state.broker.enroll(*before)
            started = time.monotonic()
            if mode == 'node-partition':
                network.cut = True
                assert not node_task.done()
            elif mode == 'leader-loss':
                leader.leader.close()  # actual retained SQL connection loss
            elif mode == 'node-replacement':
                ws, relay, node_task = await open_node()
                assert hub.get('lab-node') is not link and link.closed
            else:
                raise AssertionError('Unsupported proof mode')
            sent = []
            async def forbidden_send(operation):
                sent.append(operation.operation_id)
                raise AssertionError('Native admission occurred after authority loss')
            admission = asyncio.create_task(state.broker.dispatch(operation, forbidden_send))
            # No direct link.close(), peer.invalidate(), synthetic authority
            # callback or forged native-owner receipt is used to cause expiry.
            await require_refused_read(reader, native)
            try:
                await admission
            except p.RelayError as error:
                assert error.code is p.ErrorCode.STALE_AUTHORITY
            else:
                raise AssertionError('Post-partition broker dispatch succeeded')
            assert not sent
            broker_expired = time.monotonic() - started
            phase('read refused')
            await until(lambda: state.peer.closed and launch.peer.closed, timeout=100)
            expired = time.monotonic() - started
            phase('authority channels closed')
            if mode == 'node-partition':
                assert network.discarded > 0
                assert 25 <= broker_expired <= 40  # unchanged RuntimePeer timeout30
            await until(lambda: not factory.active)
            assert not state.owner.alive
            phase('native owner stopped')
            await until(lambda: SID not in manager.runs)
            await asyncio.gather(*tuple(manager.background))
            with c.db() as db:
                assert db.get(RelaySession, SID).sealed
                assert db.get(RelayOperation, registered.operation_id).receipt['state'] == 'uncertain'
            try:
                launch.resolve_grant(token)
            except (p.RelayError, Fenced):
                pass
            else:
                raise AssertionError('Lost placement renewed an operation grant')
            if mode == 'node-replacement':
                fresh_client = brokers.fresh(account, 'lab-node')
                raw_reader, raw_writer = await fresh_client.session_runtime({'version':2, 'enrollment':launch.token})
                stale_peer = RuntimePeer(raw_reader, raw_writer, launch.handle)
                try:
                    try:
                        await stale_peer.call('launch', {})
                    except ConnectionError:
                        pass
                    else:
                        raise AssertionError('A fresh node revived an old enrollment')
                finally:
                    await stale_peer.close()
                    await fresh_client.close()
                assert not factory.active
            # A delayed frame on the old node's real channel is discarded;
            # fresh link attachment cannot rebind the old runtime/owner.
            from acc.server.nodes import frame
            link.receive(frame(b'd', 1, b'old-authority\n'))
            assert not factory.active and SID not in manager.runs
        result = {'nativeId':native,'enrolledNodeId':node_id,'account':account,
            'leaderEpoch':leader.epoch,'nativeDigest':CLI_DIGEST,'oneTimeEnrollment':True,
            'authenticatedNode':True,'productionRelay':True,'productionNodeClient':True,'verifiedTLS':True,'badCARefused':True,'socketReplacementRefused':True,'nativeReadBeforePartition':True,
            'scenario':mode, 'partition':'bidirectional TCP blackhole' if mode == 'node-partition' else None,
            'brokerExpiredSeconds':round(broker_expired,3), 'brokerRefreshDidNotRenewAuthority':True, 'postPartitionAdmissionRefused':True, 'beforePartitionOperationVerified':True,
            'withdrawnAccountRefused':True, 'changedEnrollmentKeyRefused':True, 'oldKeyHandshakeRefused':True, 'lateOldFrameRefused':True,
            'authorityExpiredSeconds':round(expired,3),'discardedBytes':network.discarded,
            'freshNodeCannotReviveOldEnrollment':True if mode == 'node-replacement' else None,
            'operationNeverReplayed':True,'nativeOwnerStopped':not state.owner.alive,'runSealed':True,'terminalMediated':False}
        Path('/evidence/result.json').write_text(json.dumps(result))
    finally:
        phase('cleanup start')
        if admission is not None:
            if not admission.done():
                admission.cancel()
            await asyncio.gather(admission, return_exceptions=True)
        network.cut = False
        hop.close()
        await asyncio.wait_for(network.close(), 10)
        await asyncio.wait_for(hop.wait_closed(), 10)
        phase('proxy closed')
        if ha_lifetime:
            await asyncio.wait_for(ha_lifetime.__aexit__(None, None, None), 10)
            phase('HA lifetime closed')
        elif manager:
            await manager.close()
        for client, task in connections:
            task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 10)
        for running in tuple(factory.active.values()):
            await asyncio.wait_for(running.close(), 10)
        phase('owned transports closed')
        server.should_exit = True
        await asyncio.wait_for(server_task, 10)
        phase('API server closed')
        listener.close()
        await brokers.close()
        await hub.close()
        broker_server.close()
        await broker_server.wait_closed()
        leader.release()
        leader.control.dispose()
        c.engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())
