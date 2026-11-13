"""G5: SIGKILL/restart real account broker on typed simulated NodeLink.

Pinned native owner/TUI and production main enrollment; no real network node/HA.
"""
import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_session_relay_node_runtime import HOST, NEXT, SID, frame, node_runtime, until  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.node.client import Relay
from acc.server.db import write_intent
from acc.server.models import RelaySession, Session
from acc.server.nodes import FRAME
from acc.server.session_relay.hub import Hub
from acc.session_relay import protocol as p

pytestmark = pytest.mark.relay_native


@pytest.fixture
async def process_node(node_runtime):  # noqa: F811
    c = node_runtime
    source = Path(__file__).resolve().parents[1] / 'scripts/session_relay_qualification/lifetime_probe.py'
    spec = importlib.util.spec_from_file_location('lifetime_probe', source)
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    output = None
    def prepare(directory):
        nonlocal output
        with c.db() as db:
            row = db.get(Session, SID)
            config = {'workspace': row.cwd, 'run_dir': str(Path(row.run_dir).parent),
                'main_root': str(Path(c.engine.url.database).parent)}
        (directory / 'config.json').write_text(json.dumps(config))
        output = directory
    task = asyncio.create_task(asyncio.to_thread(probe.capture, prepare, timeout=12000))
    async def inspect(method):
        reader, writer = await asyncio.open_unix_connection(str(output / 'inspect.sock'))
        try:
            writer.write(json.dumps({'method': method}).encode() + b'\n')
            await writer.drain()
            result = json.loads(await asyncio.wait_for(reader.readline(), 15))
            assert 'error' not in result, result
            return result
        finally:
            writer.close()
            await writer.wait_closed()
    try:
        for _ in range(1000):
            if task.done():
                await task
            if output is not None and (output / 'ready.json').exists():
                break
            await asyncio.sleep(.02)
        assert output is not None and (output / 'ready.json').exists()
        endpoint = json.loads((output / 'ready.json').read_text())['base_url']
        @write_intent
        def configure():
            with c.db() as db:
                row = db.get(Session, SID)
                row.launch_spec.inputs = {'provider': 'codex', 'model': 'fixture-model', 'sandbox': 'read-only', 'approval': 'on-request'}
                row.launch_spec.files = {'guardrails.json': '{}', 'codex-overrides.json': json.dumps([
                    'model_provider="fixture"', 'model="fixture-model"',
                    'model_providers.fixture=' + '{name="Offline fixture",base_url="' + endpoint + '",wire_api="responses",requires_openai_auth=false,request_max_retries=0,stream_max_retries=0}'])}
                db.commit()
        configure()
        # Use the actual node channel implementation: a dead account pipe
        # closes that channel, without killing the enclosing node transport.
        with c.db() as db:
            account = db.get(Session, SID).run_as
        (output / (account + '.sock')).symlink_to('broker.sock')
        async def receive(data):
            c.wire.link.receive(data)
        relay = Relay(SimpleNamespace(send=receive), output)
        async def send(data):
            if isinstance(data, str):
                return  # fixture has no physical network heartbeat
            kind, channel = FRAME.unpack(data[:FRAME.size])
            if kind == b'o':
                assert json.loads(data[FRAME.size:]) == {'type': 'session-runtime-v1', 'account': account}
                c.wire.opens.append(channel)
                if c.wire.refuse:
                    c.wire.refuse = False
                    c.wire.link.receive(frame(b'e', channel, b'artificial first enrollment refused'))
                    return
            await relay.handle(data)
        async def broker_eof():
            await relay.aclose()
        async def close():
            c.wire.link.close()
            await relay.aclose()
            await c.wire.link.wait_closed()
        c.wire.link.preview_sender.send_raw = send
        c.wire.streams, c.wire.tasks = relay.channels, relay.tasks
        c.wire.broker_eof, c.wire.close = broker_eof, close
        c.wire.path = str(output / 'broker.sock')
        c.inspect = inspect
        yield c
    finally:
        if output is not None and (output / 'inspect.sock').exists() and not task.done():
            await c.manager.close()
            await c.wire.broker_eof()
            await inspect('finish')
        await task


@pytest.mark.parametrize('profile', ['empty', 'retained'])
async def test_os_process_restart_fences_old_grant_and_delayed_frames(process_node, record_property, monkeypatch, profile):
    c = process_node
    c.wire.refuse = True
    with pytest.raises(ConnectionError):
        await c.launch.launch(SID)
    assert not c.wire.link._channels
    assert (await c.inspect('state'))['active'] == 0
    await c.launch.launch(SID)
    old = c.launch
    state = await c.inspect('state')
    assert state['active'] == 1 and state['requests'] == 0
    assert not c.owners  # real subprocess owners, never the in-process fake
    retained = None
    if profile == 'retained':
        from relay_crash_retained import Retained
        retained = await Retained.start(c, monkeypatch)
    old_channel = c.wire.opens[-1]
    params = {'sessionId': SID, 'expectedRunId': str(old.run_id), 'text': 'Never replay killed-broker input',
        'expectedControlRevision': 1, 'expectedConfigurationRevision': 0, 'ifBusy': 'reject'}
    @write_intent
    def grant():
        with c.db() as db:
            db.get(RelaySession, SID).capabilities = {'text': 'qualified'}
            db.commit()
        registration = Hub(c.ctx).register(c.resolve, SID, str(old.run_id), 'before-kill',
            'turn.submit', p.operation_digest('turn.submit', params))
        return old.issue(c.resolve, registration.operation_id, params)
    token = grant()
    await old.peer.call('stage', {'grant': token})  # actual durable payload, never dispatch
    assert (await c.inspect('kill'))['killed'] == state['pid']
    await until(lambda: SID not in c.manager.runs)
    await asyncio.gather(*tuple(c.manager.background))
    with c.db() as db:
        assert db.get(RelaySession, SID).sealed
    await c.inspect('restart')
    restarted = await c.inspect('state')
    assert restarted['pid'] != state['pid'] and restarted['active'] == 0 and restarted['requests'] == int(retained is not None)
    # No reconnect/read automatically recreates an owner. A different, explicitly
    # reviewed session is enrolled with a fresh main grant/link/native identity.
    fresh = c.manager.new_launch(c.resolve)
    @write_intent
    def reserve():
        with c.db() as db:
            previous = db.get(Session, SID)
            db.add(Session(id=NEXT, provider='codex', name='Fresh reviewed process run', tmux_name='acc-' + NEXT,
                launch_spec_id=previous.launch_spec_id, run_as=previous.run_as, owner=previous.owner,
                team_host=HOST, workdir=previous.workdir, run_dir=str(c.factory.config.run_dir / NEXT)))
            db.flush()
            fresh.reserve(db, db.get(Session, NEXT))
            db.commit()
    reserve()
    await fresh.launch(NEXT)
    c.wire.link.receive(frame(b'd', old_channel, json.dumps({'relayVersion': 1, 'id': 'd' * 32,
        'method': 'dispatch', 'params': {'grant': token}}).encode() + b'\n'))
    with pytest.raises(ConnectionError):
        await fresh.peer.call('dispatch', {'grant': token})
    final = await c.inspect('state')
    assert final['active'] == 1 and final['requests'] == int(retained is not None)
    if retained is not None:
        await retained.after_restart(fresh, token)
        record_property('crash_retained', json.dumps(retained.proof))
    for _ in range(250):
        if not final['old_execution']:
            break
        await asyncio.sleep(.02)
        final = await c.inspect('state')
    assert final['old_execution'] == []
    assert old_channel not in c.wire.link._channels
    assert final['runs'][0]['native'] != state['runs'][0]['native']
    assert final['runs'][0]['incarnation'] != state['runs'][0]['incarnation']
    assert c.manager.runs[NEXT] is fresh
    await c.manager.close()
    for _ in range(500):
        if (await c.inspect('state'))['active'] == 0:
            break
        await asyncio.sleep(.02)
    assert (await c.inspect('state'))['active'] == 0
    record_property('scope', 'actual SIGKILL/restart broker and pinned Codex owner/TUI; typed simulated node; zero inference/replay; no HA claim')
