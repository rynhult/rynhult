"""F4: production runtime enrollment across a real typed NodeLink, fake wire/node.

The node wire forwards only fixture-account streams to the fixture's real Unix
broker. No live node, OS account service, native CLI or HA/partition is qualified.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.broker.relay_runtime import RuntimeTransportFactory
from acc.broker.server import Broker
from acc.server.db import write_intent
from acc.server.models import Node, RelaySession, Session, SessionRun
from acc.server.nodes import FRAME, NodeLink, frame
from acc.server.session_relay.hub import Hub
from acc.session_relay import protocol as p

SID = 'abc123def456'
NEXT = 'def456abc123'
HOST = 'fixture-relay-node'


async def until(predicate):
    async with asyncio.timeout(10):
        while not predicate():
            await asyncio.sleep(.01)


class NodeWire:
    def __init__(self, path, account):
        self.path, self.account = path, account
        self.streams, self.tasks, self.opens = {}, set(), []
        self.refuse = False
        self.link = NodeLink('fixture-node-id', HOST, self.send,
                             {'capabilities': ['session-runtime-v1']}, [account])

    async def pump(self, channel, reader):
        try:
            while data := await reader.read(65536):
                self.link.receive(frame(b'd', channel, data))
        finally:
            self.link.receive(frame(b'c', channel))

    async def send(self, data):
        if isinstance(data, str):
            return  # no network heartbeat is involved in this bounded fake wire
        kind, channel = FRAME.unpack(data[:FRAME.size])
        payload = data[FRAME.size:]
        if kind == b'o':
            request = json.loads(payload)
            assert request == {'type': 'session-runtime-v1', 'account': self.account}
            self.opens.append(channel)
            if self.refuse:
                self.refuse = False
                self.link.receive(frame(b'e', channel, b'artificial first enrollment refused'))
                return
            reader, writer = await asyncio.open_unix_connection(self.path)
            self.streams[channel] = writer
            self.link.receive(frame(b'k', channel))
            task = asyncio.create_task(self.pump(channel, reader))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        elif kind == b'd' and channel in self.streams:
            writer = self.streams[channel]
            writer.write(payload)
            await writer.drain()
        elif kind == b'c' and channel in self.streams:
            self.streams.pop(channel).close()

    async def broker_eof(self):
        for writer in self.streams.values():
            writer.close()
        self.streams.clear()
        await asyncio.gather(*tuple(self.tasks), return_exceptions=True)

    async def close(self):
        self.link.close()
        await self.broker_eof()
        await self.link.wait_closed()


@pytest.fixture
async def node_runtime(runtime_case):  # noqa: F811
    c = runtime_case
    with c.db() as db:
        account = db.get(Session, SID).run_as
    wire = NodeWire(str(c.factory.config.socket_path), account)
    async def close():
        await wire.close()
    c.ctx.broker.hub = SimpleNamespace(get=lambda host: wire.link if host == HOST else None, close=close)
    @write_intent
    def reserve_node():
        with c.db() as db:
            db.delete(db.get(RelaySession, SID))
            db.delete(db.get(SessionRun, c.launch.run_id))
            db.flush()
            session = db.get(Session, SID)
            session.team_host = HOST  # fixture routing snapshot; no team assignment
            db.add(Node(id='fixture-node-id', name=HOST, status='active', public_key='artificial-node-key'))
            db.commit()
        c.launch = c.manager.new_launch(c.resolve)
        with c.db() as db:
            c.launch.reserve(db, db.get(Session, SID))
            db.commit()
    reserve_node()
    c.wire = wire
    try:
        yield c
    finally:
        await wire.close()


async def test_failed_first_typed_node_enrollment_can_open_fresh_stream(node_runtime):
    c = node_runtime
    c.wire.refuse = True
    with pytest.raises(ConnectionError):
        await c.launch.launch(SID)
    assert not c.owners and not c.factory.active and not c.wire.link._channels
    await c.launch.launch(SID)  # explicit reviewed retry, no owner existed on first refusal
    assert len(c.owners) == 1 and len(c.wire.opens) == 2
    assert c.wire.opens[0] != c.wire.opens[1]
    assert c.manager.runs[SID] is c.launch
    assert c.launch.placement[0] == HOST


async def test_broker_restart_fences_old_grant_and_delayed_node_frames(node_runtime):
    c = node_runtime
    await c.launch.launch(SID)
    old_launch, old_factory = c.launch, c.factory
    old_channel = c.wire.opens[-1]
    params = {'sessionId': SID, 'expectedRunId': str(old_launch.run_id), 'text': 'Never replay this old node input',
              'expectedControlRevision': 1, 'expectedConfigurationRevision': 0, 'ifBusy': 'reject'}
    @write_intent
    def old_grant():
        with c.db() as db:
            db.get(RelaySession, SID).capabilities = {'text': 'qualified'}
            db.commit()
        registration = Hub(c.ctx).register(c.resolve, SID, str(old_launch.run_id), 'old-grant',
                                           'turn.submit', p.operation_digest('turn.submit', params))
        return old_launch.issue(c.resolve, registration.operation_id, params)
    token = old_grant()
    await c.wire.broker_eof()
    await until(lambda: SID not in c.manager.runs and not old_factory.active)
    with c.db() as db:
        assert db.get(RelaySession, SID).sealed
    # A separately instantiated broker factory is the restarted process boundary
    # in this fixture. Only the fixture Unix socket is replaced.
    config = old_factory.config
    restarted = RuntimeTransportFactory(config, owned_view_factory=None)
    server = await asyncio.start_unix_server(Broker(config, relay_transport=restarted).handle,
        path=str(config.socket_path) + '.restart', limit=1 << 20)
    restart_path = str(config.socket_path) + '.restart'
    c.wire.path = restart_path
    fresh = c.manager.new_launch(c.resolve)
    @write_intent
    def reserve_fresh():
        with c.db() as db:
            old = db.get(Session, SID)
            db.add(Session(id=NEXT, provider='codex', name='Fresh reviewed node run', tmux_name='acc-' + NEXT,
                launch_spec_id=old.launch_spec_id, run_as=old.run_as, owner=old.owner, team_host=HOST,
                workdir=old.workdir, run_dir=str(config.run_dir / NEXT)))
            db.flush()
            fresh.reserve(db, db.get(Session, NEXT))
            db.commit()
    try:
        reserve_fresh()
        await fresh.launch(NEXT)
        before = list(c.owners[-1].calls)
        c.wire.link.receive(frame(b'd', old_channel, b'{"id":"late","method":"authority.verify","params":{}}\n'))
        with pytest.raises(ConnectionError):
            await fresh.peer.call('dispatch', {'grant': token})
        assert c.owners[-1].calls == before
        assert not any(method == 'turn/start' for method, _ in c.owners[0].calls)
        assert not any(method == 'turn/start' for method, _ in c.owners[-1].calls)
        assert c.manager.runs[NEXT] is fresh
    finally:
        await c.wire.broker_eof()
        await until(lambda: not restarted.active)
        server.close()
        await server.wait_closed()


async def test_old_node_without_runtime_capability_does_not_fall_back_to_ordinary_rpc(node_runtime):
    c = node_runtime
    c.wire.link.info['capabilities'] = []
    with pytest.raises(ConnectionError, match='Node update required'):
        await c.launch.launch(SID)
    assert not c.wire.opens and not c.owners and not c.factory.active
