"""One real pinned owner/stock TUI, public cookie take and service delivery race."""
import asyncio
import base64
import importlib.util
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from test_session_relay_public_writer import attach, call
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.server import auth
from acc.server.api.context import Context
from acc.server.api.session_relay import router
from acc.server.broker_client import Brokers
from acc.server.db import write_intent
from acc.server.models import AuditLog, RelaySession, Session, User
from acc.server.orchestrator import delivery
from acc.server.session_relay import delivery as service
from acc.server.supervisor import LaunchRefused
from acc.session_relay import protocol as p

SID = 'abc123def456'


pytestmark = pytest.mark.relay_native


@pytest.mark.parametrize('winner', ['delivery', 'take'])
async def test_real_native_public_take_service_race(runtime_case, monkeypatch, record_property, winner):  # noqa: F811
    c = runtime_case
    path = Path(__file__).resolve().parents[1] / 'scripts/session_relay_qualification/codex_placement_probe.py'
    spec = importlib.util.spec_from_file_location('g4_native_race_probe', path)
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    output = None
    def prepare(directory):
        nonlocal output
        output = directory
        with c.db() as db:
            row = db.get(Session, SID)
            (directory / 'config.json').write_text(json.dumps({'workspace': row.cwd,
                'run_dir': str(Path(row.run_dir).parent), 'timeout': 180}))
    task = asyncio.create_task(asyncio.to_thread(probe.capture, prepare))
    async def inspect(method):
        reader, writer = await asyncio.open_unix_connection(str(output / 'inspect.sock'))
        try:
            writer.write(json.dumps({'method': method}).encode()+b'\n')
            await writer.drain()
            result = json.loads(await asyncio.wait_for(reader.readline(), 5))
            assert 'error' not in result, result
            return result
        finally:
            writer.close()
            await writer.wait_closed()
    brokers = None
    pending = None
    release = asyncio.Event()
    try:
        for _ in range(1000):
            if task.done():
                await task
            if output is not None and (output / 'ready.json').exists():
                break
            await asyncio.sleep(.02)
        endpoint = json.loads((output / 'ready.json').read_text())['base_url']
        @write_intent
        def configure():
            with c.db() as db:
                row = db.get(Session, SID)
                row.launch_spec.inputs = {'provider': 'codex', 'model': 'fixture-model', 'sandbox': 'read-only', 'approval': 'on-request'}
                row.launch_spec.files = {'guardrails.json': '{}', 'codex-overrides.json': json.dumps([
                    'model_provider="fixture"', 'model="fixture-model"',
                    'model_providers.fixture={name="Offline fixture",base_url="'+endpoint+'",wire_api="responses",requires_openai_auth=false,request_max_retries=0,stream_max_retries=0}'])}
                db.commit()
                return row.run_as
        account = configure()
        brokers = Brokers(output / 'broker.sock', None, account, 'fixture-services')
        supervisor = c.ctx.supervisor
        supervisor.db, supervisor.relay_manager, supervisor.broker = c.db, c.manager, brokers
        supervisor._get = lambda db, identifier: db.get(Session, identifier)
        supervisor.vault.deliver = lambda *_a, **_k: SimpleNamespace(env={}, catalog=None, check_env=lambda _: None)
        context = Context(replace(c.ctx.settings, cookie_secure=False, allowed_origins=('http://fixture',)),
            c.db, supervisor, brokers, None, 'synthetic-native-race-api')
        context.relay_runtime = c.manager
        c.ctx = c.manager.ctx = c.launch.ctx = context
        await c.launch.launch(SID)
        await inspect('qualify')
        monkeypatch.setattr(service, 'DELIVERY_QUALIFIED', True)
        monkeypatch.setattr('acc.server.session_relay.writers.PUBLIC_WRITER_QUALIFIED', True)
        monkeypatch.setattr(delivery, 'enabled', lambda: True)
        @write_intent
        def login():
            with c.db() as db:
                db.get(RelaySession, SID).capabilities = {'text': 'qualified', 'terminal.input': 'qualified'}
                token, csrf = auth.create_session(db, db.get(User, 1), 'fixture')
                db.commit()
                return token, csrf
        token, csrf = login()
        app = FastAPI()
        app.include_router(router(context))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://fixture',
            headers={'Origin': 'http://fixture', 'X-ACC-CSRF': csrf}, cookies={context.session_cookie: token}) as client:
            view = await attach(client, 'native-delivery-race', ['writer', 'terminal'])
            async def mutate(method, payload, key):
                params = {'sessionId': SID, 'expectedRunId': str(c.launch.run_id), **payload}
                registration = await call(client, 'operation.register', {'expectedRunId': str(c.launch.run_id),
                    'registrationKey': key, 'method': method, 'payloadDigest': p.operation_digest(method, params)})
                assert 'result' in registration, registration
                return await call(client, method, {**params, 'operationId': registration['result']['operationId']})
            async def take(view_name, revision, key):
                return await mutate('writer.take', {'view': view_name, 'expectedRevision': revision,
                    'attachmentId': view['attachmentId'], 'reason': 'Native G4 race'}, key)
            assert (await take('chat', 1, 'initial-chat'))['result']['state'] == 'accepted'
            @write_intent
            def enqueue():
                return delivery.enqueue(supervisor, SID, '@api', 'First production Chat', relay_service=True)
            receipt = None
            if winner == 'delivery':
                await inspect('hold_model')
                message = enqueue()
                await delivery._deliver(supervisor, message['id'])
                for _ in range(500):
                    state = await inspect('state')
                    if state['modelHeld'] and state['active_turn']:
                        break
                    await asyncio.sleep(.02)
                assert state['modelHeld'] and state['active_turn']
                with c.db() as db:
                    receipt = dict(service.operation(db, message['id']).receipt)
                    assert receipt['state'] == 'accepted' and receipt['actor']['kind'] == 'service'
                refused = await take('terminal', 2, 'busy-take')
                assert refused['error']['data']['code'] == 'BUSY'
                with c.db() as db:
                    assert db.get(RelaySession, SID).control_revision == 2
                    assert service.operation(db, message['id']).receipt == receipt
                await inspect('release_model')
                for _ in range(500):
                    if (await inspect('state'))['active_turn'] is None:
                        break
                    await asyncio.sleep(.02)
                else:
                    raise AssertionError('Native service turn did not finish')
                assert (await take('terminal', 2, 'settled-take'))['result']['state'] == 'accepted'
                await delivery._deliver(supervisor, message['id'])
                assert (await inspect('state'))['requests'] == 1
            else:
                reached = asyncio.Event()
                original = c.launch.peer.call
                async def held(method, params):
                    if method == 'writer.take':
                        reached.set()
                        await asyncio.wait_for(release.wait(), 15)
                    return await original(method, params)
                monkeypatch.setattr(c.launch.peer, 'call', held)
                pending = asyncio.create_task(take('terminal', 2, 'take-first'))
                await asyncio.wait_for(reached.wait(), 5)
                with pytest.raises(LaunchRefused):
                    enqueue()
                release.set()
                assert (await asyncio.wait_for(pending, 15))['result']['state'] == 'accepted'
                monkeypatch.setattr(c.launch.peer, 'call', original)
            control = await mutate('control.claim', {'expectedRevision': 3, 'attachmentId': view['attachmentId']}, 'fresh-terminal')
            assert 'result' in control, control
            grant = control['result']
            frame = await call(client, 'terminal.input', {'attachmentId': view['attachmentId'], 'inputGrant': grant['inputGrant'],
                'inputEpoch': grant['inputEpoch'], 'frameSeq': 1, 'data': base64.b64encode(b'Fresh race unsent draft').decode()})
            assert 'result' in frame, frame
            for _ in range(500):
                screen = await inspect('screen')
                if 'Fresh race unsent draft' in screen['text']:
                    break
                await asyncio.sleep(.02)
            assert 'Fresh race unsent draft' in screen['text']
            with pytest.raises(LaunchRefused):
                enqueue()
            assert 'Fresh race unsent draft' in (await inspect('screen'))['text']
            with c.db() as db:
                assert db.get(RelaySession, SID).control_revision == 3
                audits = db.scalars(select(AuditLog).where(AuditLog.action == 'relay.writer.take')).all()
                assert len(audits) == 2 and audits[-1].detail['previous']['principal'] == 'alex'
                assert audits[-1].detail['previous']['channel'] == 'chat'
                if receipt:
                    event = db.scalars(select(AuditLog).where(AuditLog.action == 'relay.delivery.settled')).one()
                    assert event.actor == '@api' and event.detail['channel'] == 'api'
                    assert event.detail['previous']['principal'] == 'alex'
            record_property('winner', winner)
            record_property('service_receipt', json.dumps(receipt))
        await c.manager.close()
        await inspect('finish')
        result = await task
        assert len(result['requests']) == (1 if winner == 'delivery' else 0)
        assert result['terminalMediated'] is False
        record_property('native_requests', len(result['requests']))
        record_property('scope', 'real pinned Codex0.167.1/stock TUI/account broker/main/cookie HTTP take; held native model or pre-native-RPC retirement boundary; synthetic model/person, no browser UI or TLS claim')
    finally:
        release.set()
        if pending is not None and not pending.done():
            await asyncio.gather(pending, return_exceptions=True)
        await c.manager.close()
        if output is not None and (output / 'inspect.sock').exists() and not task.done():
            await inspect('release_model')
            await inspect('finish')
        await task
        if brokers:
            await brokers.close()
