"""Real production Codex broker/stock remote TUI and public cookie writer path."""
import asyncio
import base64
import importlib.util
import json
import time
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
from acc.server.models import AuditLog, RelayOperation, RelaySession, Session, User
from acc.session_relay import protocol as p

SID = 'abc123def456'


pytestmark = pytest.mark.relay_native


@pytest.mark.parametrize('cold_first,lost_processing,permission',
                         [(False, False, False), (True, False, False), (False, True, False), (False, False, True), (False, False, 'rollback'), (False, False, 'fast'), (False, 'delivery-ack', False), (False, 'expired-connect', False), (False, 'stopped-connect', False), (False, 'revoked-connect', False), (False, 'client-exit', False), (False, 'owner-exit', False), (False, 'public-interrupt', False), (False, 'public-interrupt-lost', False), (False, 'owner-normal', False), (False, 'public-interrupt-expired-write', False), (False, 'exit-drain', False), (False, 'exit-drain-timeout', False), (False, 'public-interrupt-broker-lost', False), (False, 'terminal-interrupt', False), (False, 'terminal-interrupt-broker-lost', False), (False, 'public-interrupt-write-budget', False), (False, 'public-interrupt-write-policy', False), (False, 'public-interrupt-write-stop', False), (False, 'public-interrupt-write-target', False), (False, 'public-interrupt-write-flag', False), (False, 'partial-header', False), (False, 'partial-body', False), (False, 'read-eof', False), (False, 'capacity-owner', False), (False, 'capacity-global', False), (False, 'capacity-native-socket', False)],
                         ids=['warm', 'empty-first', 'lost-processing', 'permission', 'permission-rollback', 'permission-immediate', 'lost-delivery-ack', 'expired-connect', 'stopped-connect', 'revoked-connect', 'client-exit', 'owner-exit', 'public-interrupt', 'public-interrupt-lost', 'owner-normal', 'public-interrupt-expired-write', 'exit-drain', 'exit-drain-timeout', 'public-interrupt-broker-lost', 'terminal-interrupt', 'terminal-interrupt-broker-lost', 'public-interrupt-write-budget', 'public-interrupt-write-policy', 'public-interrupt-write-stop', 'public-interrupt-write-target', 'public-interrupt-write-flag', 'partial-header', 'partial-body', 'read-eof', 'capacity-owner', 'capacity-global', 'capacity-native-socket'])
async def test_production_codex_native_public_take_and_input(runtime_case, monkeypatch, record_property, cold_first, lost_processing, permission):  # noqa: F811
    c = runtime_case
    source = Path(__file__).resolve().parents[1] / 'scripts/session_relay_qualification/codex_placement_probe.py'
    spec = importlib.util.spec_from_file_location('codex_placement_probe', source)
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    output = None
    latency = []
    def prepare(directory):
        nonlocal output
        output = directory
        with c.db() as db:
            row = db.get(Session, SID)
            config = {'workspace': row.cwd, 'run_dir': str(Path(row.run_dir).parent)}
        if getattr(c, 'native_generation_capacity', False):
            config['timeout'] = 220
        (directory / 'config.json').write_text(json.dumps(config))
    task = asyncio.create_task(asyncio.to_thread(probe.capture, prepare))
    async def inspect(method):
        reader, writer = await asyncio.open_unix_connection(str(output / 'inspect.sock'))
        try:
            writer.write(json.dumps({'method': method}).encode() + b'\n')
            await writer.drain()
            result = json.loads(await asyncio.wait_for(reader.readline(), 5))
            assert 'error' not in result, result
            return result
        finally:
            writer.close()
            await writer.wait_closed()
    brokers = None
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
                return row.run_as
        account = configure()
        brokers = Brokers(output / 'broker.sock', None, account, 'fixture-services')
        c.ctx.broker = brokers
        c.ctx.supervisor.vault.deliver = lambda *_a, **_kw: SimpleNamespace(env={}, catalog=None, check_env=lambda _: None)
        context = Context(replace(c.ctx.settings, cookie_secure=False, allowed_origins=('http://fixture',)),
            c.db, c.ctx.supervisor, brokers, None, 'fixture-api-token')
        context.relay_runtime = c.manager
        c.manager.ctx = c.launch.ctx = context
        c.ctx = context
        await c.launch.launch(SID)
        await inspect('qualify')
        @write_intent
        def login():
            with c.db() as db:
                db.get(RelaySession, SID).capabilities = {'text': 'qualified', 'terminal.input': 'qualified',
                    **({'decision.tool_approval': 'qualified'} if permission else {})}
                token, csrf = auth.create_session(db, db.get(User, 1), 'fixture')
                db.commit()
                return token, csrf
        token, csrf = login()
        monkeypatch.setattr('acc.server.session_relay.writers.PUBLIC_WRITER_QUALIFIED', True)
        app = FastAPI()
        app.include_router(router(context))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://fixture',
            headers={'Origin': 'http://fixture', 'X-ACC-CSRF': csrf}, cookies={context.session_cookie: token}) as client:
            view = await attach(client, 'native-public-person', ['writer', 'terminal', 'decisions'])
            native_screens = []
            async def capture_screen(phase):
                response = await client.get(f'/api/sessions/{SID}/relay/terminal', params={
                    'attachmentId': view['attachmentId'], 'expectedRunId': str(c.launch.run_id)})
                assert response.status_code == 200, response.text
                value = response.json()
                native_screens.append({'phase': phase, 'text': value['text'], 'truncated': value['truncated']})
            async def mutate(method, payload, key, using=client):
                params = {'sessionId': SID, 'expectedRunId': str(c.launch.run_id), **payload}
                registration_started = time.perf_counter()
                registered = await call(using, 'operation.register', {'expectedRunId': str(c.launch.run_id),
                    'registrationKey': key, 'method': method, 'payloadDigest': p.operation_digest(method, params)})
                assert 'result' in registered, registered
                registration_ms = (time.perf_counter() - registration_started) * 1000
                dispatch_started = time.perf_counter()
                value = await call(using, method, {**params, 'operationId': registered['result']['operationId']})
                latency.append({'method': method, 'registration_ms': registration_ms,
                    'dispatch_ms': (time.perf_counter() - dispatch_started) * 1000})
                assert 'result' in value, value
                if method in ('turn.submit', 'writer.take'):
                    assert value['result']['state'] == 'accepted', value
                return value['result']
            async def idle():
                for _ in range(500):
                    value = await inspect('state')
                    if value['active_turn'] is None:
                        return value
                    await asyncio.sleep(.02)
                raise AssertionError(value)
            async def unexpected_codex_exit(stage, decision=None, control=None):
                kind = c.native_provider_exit
                before = await inspect('exit_state')
                assert (await inspect({'vendor': 'crash_producer_vendor', 'view': 'crash_view', 'view-supervisor': 'crash_view_supervisor'}[kind]))['killed' if kind == 'vendor' else 'exited']
                for _ in range(500):
                    ended = await inspect('exit_state')
                    if kind != 'vendor' or (ended['root_returncode'] is not None and ended['rpc_closed']):
                        break
                    await asyncio.sleep(.01)
                assert ended['owner'] == before['owner'] and before['active'] == 1 and ended['active'] <= 1
                if kind == 'vendor':
                    assert ended['root_returncode'] is not None and ended['rpc_closed'] and ended['empty']
                if control is not None:
                    for _ in range(2):
                        reply = await call(client, 'terminal.input', {'attachmentId': view['attachmentId'],
                            'inputGrant': control['inputGrant'], 'inputEpoch': control['inputEpoch'],
                            'frameSeq': 3 if stage != 'idle' else 1, 'data': base64.b64encode(b'\r').decode()})
                        assert 'error' in reply, reply
                if decision is not None:
                    answer = {'sessionId': SID, 'expectedRunId': str(c.launch.run_id),
                        'decisionId': decision['decisionId'], 'choiceId': 'accept' if kind == 'vendor' else 'cancel',
                        'expectedRevision': decision['revision'], 'actionDigest': decision['actionDigest']}
                    registered = await call(client, 'operation.register', {'expectedRunId': str(c.launch.run_id),
                        'registrationKey': 'exited-native-allow', 'method': 'decision.reply',
                        'payloadDigest': p.operation_digest('decision.reply', answer)})
                    if 'result' in registered:
                        reply = await call(client, 'decision.reply', {**answer, 'operationId': registered['result']['operationId']})
                        assert 'error' in reply or reply.get('result', {}).get('state') != 'accepted', reply
                        with c.db() as db:
                            receipt = db.get(RelayOperation, registered['result']['operationId']).receipt
                            assert receipt['state'] != 'accepted' and receipt['actor']['principal'] == 'alex'
                    else:
                        assert 'error' in registered  # exit may already withdraw the decision
                assert not (await inspect('permission_state'))['marker_exists']
                await inspect('release_model')
                from acc.server.session_relay.hub import seal_for_stop
                @write_intent
                def stop_intent():
                    seal_for_stop(c.ctx, SID)
                    with c.db() as db:
                        db.get(Session, SID).desired_state = 'stopped'
                        db.commit()
                stop_intent()
                confirmed = await c.manager.stop(SID)
                retired_path = output / 'retired-stop.json'
                if confirmed:
                    retired = json.loads(retired_path.read_text())
                    assert retired['result'] == {'exited': True, 'wholeTree': 'enclosure-v1'}
                    assert retired['producer'] and all(retired['views']) and retired['active'] == 0
                else:
                    # Unexpected provider loss invalidates the RPC link before
                    # Stop can get a typed ACK; do not invent a confirmed Stop.
                    assert not retired_path.exists() or json.loads(retired_path.read_text())['result']['exited'] is False
                await c.manager.close()
                for _ in range(500):
                    cleanup = await inspect('cleanup_state')
                    if cleanup['active'] == 0:
                        break
                    await asyncio.sleep(.01)
                assert cleanup['active'] == 0 and cleanup['producer'] and cleanup['views']
                if kind == 'view-supervisor':
                    assert not confirmed and not all(cleanup['views'])  # missing supervisor cannot prove empty tree
                else:
                    assert all(cleanup['views'])
                await inspect('finish')
                result = await task
                assert len(result['requests']) == before['requests']
                assert result['terminalMediated'] is False
                record_property('native_provider_exit', f'actual Codex {kind} loss at {stage}; original owner retained/no replacement, next native input refuses; dead producer answers refuse, live producer permits person cancellation; no automatic answer/effect/replay; honest Stop plus separate whole-tree cleanup')

            initial_revision = 1
            if not cold_first:
                await mutate('writer.take', {'view': 'chat', 'reason': 'Person chooses initial Chat input',
                    'expectedRevision': initial_revision, 'attachmentId': view['attachmentId']}, 'initial-chat')
                initial_revision += 1
                if getattr(c, 'native_submit_revoke', False):
                    params = {'sessionId': SID, 'expectedRunId': str(c.launch.run_id),
                        'text': 'Never send after Codex cookie loss', 'expectedControlRevision': initial_revision,
                        'expectedConfigurationRevision': 0, 'ifBusy': 'reject'}
                    registered = await call(client, 'operation.register', {'expectedRunId': str(c.launch.run_id),
                        'registrationKey': 'codex-revoked-submit', 'method': 'turn.submit',
                        'payloadDigest': p.operation_digest('turn.submit', params)})
                    assert 'result' in registered, registered
                    original = c.launch.peer.handle
                    seen = False
                    async def revoke_after_verify(method, payload):
                        nonlocal seen
                        result = await original(method, payload)
                        if method == 'authority.verify' and not seen:
                            seen = True
                            @write_intent
                            def revoke():
                                with c.db() as db:
                                    auth.end_session(db, token)
                                    db.commit()
                            revoke()
                        return result
                    c.launch.peer.handle = revoke_after_verify
                    try:
                        response = await client.post(f'/api/sessions/{SID}/relay', json={
                            'jsonrpc': '2.0', 'id': 'codex-revoked-send', 'method': 'ry/turn.submit',
                            'params': {**params, 'operationId': registered['result']['operationId']}})
                        assert response.status_code in (200, 401, 403), response.text
                        assert response.status_code != 200 or 'error' in response.json(), response.text
                    finally:
                        c.launch.peer.handle = original
                    assert seen and (await inspect('state'))['requests'] == 0
                    with c.db() as db:
                        assert db.get(RelayOperation, registered['result']['operationId']).receipt['state'] != 'accepted'
                    await c.manager.close()
                    await inspect('finish')
                    result = await task
                    assert result['requests'] == [] and result['terminalMediated'] is False
                    record_property('native_inflight_codex_revocation', 'actual app-server/stock TUI: cookie revoked after first exact dispatch verifier; final barrier refuses before native turn/start; no model request or accepted receipt')
                    return
                if str(lost_processing).startswith('exit-drain'):
                    await inspect('hold_message_publication')
                response_started = time.perf_counter()
                await mutate('turn.submit', {'text': 'First production Chat', 'expectedControlRevision': initial_revision,
                    'expectedConfigurationRevision': 0, 'ifBusy': 'reject'}, 'first-chat')
                if str(lost_processing).startswith('exit-drain'):
                    drained = lost_processing == 'exit-drain'
                    for _ in range(500):
                        if (await inspect('message_publication_state'))['waiting']:
                            break
                        await asyncio.sleep(.01)
                    assert (await inspect('message_publication_state'))['waiting']
                    assert await inspect('end_owner') == {'exited': True, 'returnCode': 0}
                    try:
                        if drained:
                            await asyncio.sleep(.1)
                            with c.db() as db:
                                assert not db.get(RelaySession, SID).sealed
                            await inspect('release_message_publication')
                        for _ in range(600):
                            snap = (await call(client, 'session.snapshot', {}))['result']
                            if snap.get('exitOutcome') == 'normal':
                                break
                            await asyncio.sleep(.01)
                        assert snap['exitOutcome'] == 'normal' and snap['finalEventsDrained'] is drained
                        replay = (await call(client, 'events.read', {'attachmentId': view['attachmentId']}))['result']['events']
                        final = next(event for event in replay if event['payload'].get('exitOutcome'))
                        messages = [event for event in replay if event['kind'] == 'message.upsert'
                            and event['payload'].get('role') == 'assistant']
                        if drained:
                            assert messages and messages[-1]['payload']['text'] == 'Production fixture response 1.'
                            assert messages[-1]['eventSeq'] < final['eventSeq']
                            assert any(event['kind'] == 'turn.completed' and event['eventSeq'] < final['eventSeq'] for event in replay)
                        else:
                            assert not messages
                        record_property('native_final_drain', 'drained' if drained else 'incomplete')
                    finally:
                        await inspect('release_message_publication')
                    await c.manager.close()
                    await inspect('finish')
                    result = await task
                    assert len(result['requests']) == 1 and result['terminalMediated'] is False
                    return
                await idle()
                latency.append({'method': 'first-chat-response', 'response_ms': (time.perf_counter() - response_started) * 1000})
            if str(lost_processing).startswith('public-interrupt'):
                lost = lost_processing == 'public-interrupt-lost'
                broker_lost = lost_processing == 'public-interrupt-broker-lost'
                await inspect('lose_broker_interrupt_answer' if broker_lost else 'lose_interrupt_answer' if lost else 'qualify_interrupt')
                @write_intent
                def qualify_interrupt():
                    with c.db() as db:
                        row = db.get(RelaySession, SID)
                        row.capabilities = {**row.capabilities, 'interrupt': 'qualified'}
                        db.commit()
                qualify_interrupt()
                await inspect('hold_model')
                active = await mutate('turn.submit', {'text': 'Actual public interrupt target',
                    'expectedControlRevision': initial_revision, 'expectedConfigurationRevision': 0}, 'interrupt-target')
                for _ in range(500):
                    current = await inspect('state')
                    if current['modelHeld'] and current['active_turn'] == active['operationId']:
                        break
                    await asyncio.sleep(.02)
                assert current['modelHeld'] and current['active_turn'] == active['operationId']
                snap = (await call(client, 'session.snapshot', {}))['result']
                assert snap['activeTurnId'] == active['operationId']
                if lost_processing == 'public-interrupt-expired-write' or str(lost_processing).startswith('public-interrupt-write-'):
                    await inspect('hold_native_write')
                    params = {'sessionId': SID, 'expectedRunId': str(c.launch.run_id),
                        'turnId': active['operationId'], 'expectedControlRevision': snap['controlRevision']}
                    registered = await call(client, 'operation.register', {'expectedRunId': str(c.launch.run_id),
                        'registrationKey': 'write-expiry', 'method': 'turn.interrupt',
                        'payloadDigest': p.operation_digest('turn.interrupt', params)})
                    assert 'result' in registered, registered
                    identifier = registered['result']['operationId']
                    pending = asyncio.create_task(call(client, 'turn.interrupt', {**params, 'operationId': identifier}))
                    try:
                        for _ in range(500):
                            waiting = await inspect('native_write_state')
                            if waiting['waiting']:
                                break
                            await asyncio.sleep(.02)
                        assert waiting == {'waiting': True, 'interrupts': 0}
                        @write_intent
                        def expire():
                            from datetime import timedelta

                            from acc.server.models import utcnow
                            with c.db() as db:
                                if lost_processing.endswith('-budget'):
                                    from acc.server.models import Observation, Setting
                                    db.merge(Setting(key='budget.alex', value={'rules': {'codex.primary': {
                                        'mode': 'cap', 'limit': 1, 'action': 'pause'}}}))
                                    db.merge(Observation(key='codex.rate_limits', source='offline-fixture', observed_at=time.time(),
                                        value={'primary': {'used_percent': 99, 'resets_at': time.time() + 3600, 'window_minutes': 300}}))
                                elif lost_processing.endswith('-policy'):
                                    from acc.server.models import SecurityRevision
                                    revision = db.get(SecurityRevision, 1)
                                    if revision is None:
                                        db.add(SecurityRevision(id=1, revision=1))
                                    else:
                                        revision.revision += 1
                                elif lost_processing.endswith('-stop'):
                                    db.get(Session, SID).desired_state = 'stopped'
                                elif lost_processing.endswith('-target'):
                                    db.get(RelaySession, SID).active_operation_id = 'replacement-operation'
                                elif lost_processing.endswith('-flag'):
                                    c.ctx.settings = replace(c.ctx.settings, feature_overrides={'session_relay': False})
                                else:
                                    db.get(RelayOperation, identifier).deadline = utcnow() - timedelta(seconds=1)
                                db.commit()
                        expire()
                    finally:
                        await inspect('release_native_write')
                    refused = await pending
                    assert refused['result']['state'] == 'failed', refused
                    assert (await inspect('native_write_state'))['interrupts'] == 0
                    assert (await inspect('state'))['active_turn'] == active['operationId']
                    await inspect('release_model')
                    await idle()
                    native = await inspect('interrupt_state')
                    assert native['interrupts'] == [] and native['writeAdmissions'] == []
                    if (await inspect('state'))['active']:
                        assert native['completions'][0]['status'] == 'completed'
                    else:
                        with c.db() as db:
                            assert db.get(RelaySession, SID).sealed
                    replay = await call(client, 'turn.interrupt', {**params, 'operationId': identifier})
                    assert replay == refused or 'error' in replay
                    record_property('native_write_expiry', json.dumps(native))
                    record_property('native_final_write_refusal', str(lost_processing))
                    await c.manager.close()
                    await inspect('finish')
                    result = await task
                    assert len(result['requests']) == 2 and result['terminalMediated'] is False
                    return
                receipt = await mutate('turn.interrupt', {'turnId': active['operationId'],
                    'expectedControlRevision': snap['controlRevision']}, 'public-interrupt')
                assert receipt['state'] == ('uncertain' if lost or broker_lost else 'accepted'), receipt
                await idle()
                native = await inspect('interrupt_state')
                assert len(native['interrupts']) == 1 and len(native['completions']) == 1, native
                assert native['interrupts'][0]['turnId'] == native['completions'][0]['id']
                assert native['completions'][0]['status'] == 'interrupted', native
                replay = await call(client, 'turn.interrupt', {'sessionId': SID, 'expectedRunId': str(c.launch.run_id),
                    'turnId': active['operationId'], 'expectedControlRevision': snap['controlRevision'],
                    'operationId': receipt['operationId']})
                assert replay['result'] == receipt, replay
                history = await call(client, 'events.read', {'attachmentId': view['attachmentId']})
                assert any(event['kind'] == 'turn.interrupted' and event.get('operationId') == active['operationId']
                    for event in history['result']['events']), history
                assert len((await inspect('interrupt_state'))['interrupts']) == 1
                if broker_lost:
                    observed = (await call(client, 'operation.get', {'operationId': receipt['operationId']}))['result']
                    assert observed['state'] == 'accepted', observed
                    assert observed['actor'] == receipt['actor'] and observed['payloadDigest'] == receipt['payloadDigest']
                    again = (await call(client, 'operation.get', {'operationId': receipt['operationId']}))['result']
                    assert again == observed
                    assert len((await inspect('interrupt_state'))['interrupts']) == 1
                    record_property('broker_receipt_observed', 'accepted-without-resend')
                record_property('native_interrupt', json.dumps({'outcome': receipt['state'], 'native': native}))
                await inspect('release_model')
                await c.manager.close()
                await inspect('finish')
                result = await task
                assert len(result['requests']) == 2 and result['terminalMediated'] is False
                return
            if lost_processing in ('owner-exit', 'owner-normal'):
                from acc.server.models import SessionRun
                await mutate('writer.take', {'view': 'terminal', 'reason': 'Prepare a real old Terminal grant',
                    'expectedRevision': initial_revision, 'attachmentId': view['attachmentId']}, 'exit-terminal')
                control = await mutate('control.claim', {'expectedRevision': initial_revision + 1,
                    'attachmentId': view['attachmentId']}, 'exit-claim')
                take_payload = {'sessionId': SID, 'expectedRunId': str(c.launch.run_id),
                    'view': 'chat', 'reason': 'Old run cannot take', 'expectedRevision': initial_revision + 1,
                    'attachmentId': view['attachmentId']}
                registered = await call(client, 'operation.register', {'expectedRunId': str(c.launch.run_id),
                    'registrationKey': 'old-take', 'method': 'writer.take',
                    'payloadDigest': p.operation_digest('writer.take', take_payload)})
                assert 'result' in registered, registered
                normal = lost_processing == 'owner-normal'
                assert (await inspect('end_owner' if normal else 'crash_owner')) == {'exited': True, 'returnCode': 0 if normal else -9}
                for _ in range(500):
                    with c.db() as db:
                        sealed = db.get(RelaySession, SID).sealed
                        ended_state = db.get(Session, SID).observed_state
                    if sealed and ended_state == ('stopped' if normal else 'recovery_needed'):
                        break
                    await asyncio.sleep(.02)
                assert sealed and ended_state == ('stopped' if normal else 'recovery_needed')
                # Current attachments can observe retained history after EOF;
                # their old mutation authority cannot survive it.
                snap = await call(client, 'session.snapshot', {})
                assert 'result' in snap and snap['result']['writer']['active'] is False
                replay = await call(client, 'events.read', {'attachmentId': view['attachmentId']})
                assert 'result' in replay, replay
                ends = [event['payload'] for event in replay['result']['events']
                        if 'exitOutcome' in event.get('payload', {})]
                assert ends and ends[-1]['exitOutcome'] == ('normal' if normal else 'abnormal')
                with c.db() as db:
                    native = db.get(SessionRun, c.launch.run_id)
                    assert native.ended_at is not None
                    assert (native.exit_status, native.exit_signal) == ((0, None) if normal else (None, 9))
                old_take = await call(client, 'writer.take', {**take_payload,
                    'operationId': registered['result']['operationId']})
                assert old_take['error']['data']['code'] != 'INVALID_PARAMS', old_take
                input_params = {'attachmentId': view['attachmentId'], 'inputGrant': control['inputGrant'],
                    'inputEpoch': control['inputEpoch'], 'frameSeq': 1,
                    'data': base64.b64encode(b'no replay').decode()}
                old_input = await call(client, 'terminal.input', input_params)
                assert old_input['error']['code'] != -32602, old_input
                old_drain = await call(client, 'terminal.drain', {
                    'attachmentId': view['attachmentId'], 'inputGrant': control['inputGrant']})
                assert old_drain['error']['code'] != -32602, old_drain
                final_screen = await client.get(f'/api/sessions/{SID}/relay/terminal', params={
                    'attachmentId': view['attachmentId'], 'expectedRunId': str(c.launch.run_id)})
                assert final_screen.status_code == 200, final_screen.text
                assert 'First production Chat' in final_screen.json()['text']
                assert ends[-1]['finalScreenAvailable'] is True
                assert len(final_screen.content) < 128 << 10
                assert SID not in c.manager.runs  # read requires no live broker or launch

                record_property('native_exit_outcome', ends[-1]['exitOutcome'])
                @write_intent
                def logout():
                    with c.db() as db:
                        auth.end_session(db, token)
                        db.commit()
                logout()
                denied = await client.get(f'/api/sessions/{SID}/relay/events')
                assert denied.status_code == 401 and 'Production fixture response' not in denied.text
                await c.manager.close()
                await inspect('finish')
                result = await task
                assert len(result['requests']) == 1
                assert result['terminalMediated'] is False
                return
            if getattr(c, 'native_history_fault', None):
                from relay_native_history_faults import qualify_history_fault
                before = await inspect('exit_state')
                await qualify_history_fault(c, client, SID, c.native_history_fault)
                after = await inspect('exit_state')
                assert before['owner'] == after['owner'] and before['requests'] == after['requests'] == 1
                assert after['root_returncode'] is None and not after['rpc_closed']
                await c.manager.close()
                await inspect('finish')
                result = await task
                assert len(result['requests']) == 1 and result['terminalMediated'] is False
                record_property('native_history_recovery', f'{c.native_history_fault}: stale cursor/epoch refuses, fresh public read attachment recovers history without launch/input/receipt replay; same actual Codex owner')
                return
            if getattr(c, 'native_stdio_fault', None):
                kind = c.native_stdio_fault
                before = await inspect('state')
                baseline = (await call(client, 'session.snapshot', {}))['result']['receipts']
                payload = {'sessionId': SID, 'expectedRunId': str(c.launch.run_id),
                    'text': 'Native stdio boundary only once', 'expectedControlRevision': initial_revision,
                    'expectedConfigurationRevision': 0, 'ifBusy': 'reject'}
                registration = await call(client, 'operation.register', {'expectedRunId': str(c.launch.run_id),
                    'registrationKey': 'native-stdio-fault', 'method': 'turn.submit',
                    'payloadDigest': p.operation_digest('turn.submit', payload)})
                assert 'result' in registration, registration
                await inspect('stdio_fault_' + kind)
                operation_id = registration['result']['operationId']
                response = await call(client, 'turn.submit', {**payload, 'operationId': operation_id})
                assert 'error' in response or response['result']['state'] != 'accepted', response
                for _ in range(200):
                    wire = await inspect('stdio_fault_state')
                    if kind.startswith('write-') or wire['responses'] == 1:
                        break
                    await asyncio.sleep(.01)
                assert wire['writes'] == 1 and wire['bytes'] > 0
                assert wire['responses'] == (0 if kind.startswith('write-') else 1)
                if not kind.startswith('write-'):
                    assert wire['native_result']  # actual native admission reply was lost/truncated
                with c.db() as db:
                    original_receipt = dict(db.get(RelayOperation, operation_id).receipt)
                    assert original_receipt['state'] != 'accepted' and original_receipt['actor']['principal'] == 'alex'
                    assert all(db.get(RelayOperation, receipt['operationId']).receipt == receipt for receipt in baseline)
                await call(client, 'turn.submit', {**payload, 'operationId': operation_id})
                await asyncio.sleep(.2)
                assert (await inspect('stdio_fault_state'))['writes'] == 1
                with c.db() as db:
                    assert db.get(RelayOperation, operation_id).receipt == original_receipt
                await c.manager.close()
                await inspect('finish')
                result = await task
                assert before['requests'] <= len(result['requests']) <= before['requests'] + (0 if kind.startswith('write-') else 1)
                assert result['terminalMediated'] is False
                record_property('native_stdio_fault', f'{kind}: actual app-server stdin/stdout boundary; one turn/start, honest original unknown receipt/no replay; prior receipts unchanged')
                return
            if getattr(c, 'native_generation_capacity', False):
                stats = await inspect('generation_state')
                while stats['count'] < 16:
                    snapshot = (await call(client, 'session.snapshot', {}))['result']
                    target = 'terminal' if snapshot['writer']['view'] == 'chat' else 'chat'
                    previous_count = stats['count']
                    await mutate('writer.take', {'view': target, 'reason': 'Person exercises retained client boundary',
                        'expectedRevision': snapshot['controlRevision'], 'attachmentId': view['attachmentId']},
                        'generation-' + str(previous_count))
                    if target == 'terminal':
                        current = (await call(client, 'session.snapshot', {}))['result']
                        await mutate('control.claim', {'expectedRevision': current['controlRevision'],
                            'attachmentId': view['attachmentId']}, 'generation-claim-' + str(previous_count))
                    stats = await inspect('generation_state')
                    assert stats['count'] == previous_count + 1
                    assert len(set(stats['names'])) == stats['count'] and all(stats['live'])
                    assert sum(stats['retired']) == stats['count'] - 1 and not stats['retired'][-1]
                    assert stats['requests'] == 1
                assert all((await inspect('generation_capture'))['readable'])
                snapshot = (await call(client, 'session.snapshot', {}))['result']
                payload = {'sessionId': SID, 'expectedRunId': str(c.launch.run_id),
                    'view': 'terminal' if snapshot['writer']['view'] == 'chat' else 'chat',
                    'reason': 'Person hits bounded retained view limit',
                    'expectedRevision': snapshot['controlRevision'], 'attachmentId': view['attachmentId']}
                registered = await call(client, 'operation.register', {'expectedRunId': str(c.launch.run_id),
                    'registrationKey': 'generation-overflow', 'method': 'writer.take',
                    'payloadDigest': p.operation_digest('writer.take', payload)})
                assert 'result' in registered, registered
                refused = await call(client, 'writer.take', {**payload, 'operationId': registered['result']['operationId']})
                assert 'error' in refused, refused
                after = await inspect('generation_state')
                assert after['count'] == 16 and after['names'] == stats['names'] and after['requests'] == 1
                assert after['recovery']  # no automatic old-lease restoration after a failed take
                with c.db() as db:
                    assert db.get(RelayOperation, registered['result']['operationId']).receipt['state'] != 'accepted'
                from acc.server.session_relay.hub import seal_for_stop
                @write_intent
                def stop_intent():
                    seal_for_stop(c.ctx, SID)
                    with c.db() as db:
                        db.get(Session, SID).desired_state = 'stopped'
                        db.commit()
                stop_intent()
                assert await c.manager.stop(SID) is False  # failed prepared take fenced the link; no typed Stop ACK
                for _ in range(1000):
                    cleanup = await inspect('cleanup_state')
                    if cleanup['active'] == 0:
                        break
                    await asyncio.sleep(.01)
                assert cleanup['active'] == 0 and cleanup['producer'] and len(cleanup['views']) == 16 and all(cleanup['views'])
                await c.manager.close()
                await inspect('finish')
                result = await task
                assert len(result['requests']) == 1 and result['terminalMediated'] is False
                record_property('native_generation_capacity', 'sixteen actual stock TUI generations, fifteen permanently retired clients still readable; seventeenth refuses without new client/lease or replay; failed prepared take fences run; Stop unconfirmed on lost link, independent cleanup proves all16 views and producer empty')
                return
            last_chat_response = 'Production fixture response ' + ('2.' if cold_first else '3.')
            receipt = await mutate('writer.take', {'view': 'terminal', 'reason': 'Person switches to native Terminal',
                'expectedRevision': initial_revision, 'attachmentId': view['attachmentId']}, 'to-terminal')
            assert receipt['state'] == 'accepted'
            await capture_screen('idle before Terminal input; real native metadata idle')
            terminal_interrupt = str(lost_processing).startswith('terminal-interrupt')
            if terminal_interrupt:
                await inspect('lose_broker_interrupt_answer' if lost_processing.endswith('-broker-lost') else 'qualify_interrupt')
                @write_intent
                def qualify_terminal_interrupt():
                    with c.db() as db:
                        row = db.get(RelaySession, SID)
                        row.capabilities = {**row.capabilities, 'interrupt': 'qualified'}
                        db.commit()
                qualify_terminal_interrupt()
            if permission != 'fast':
                await inspect('hold_model')
            if permission:
                await inspect('request_permission')
                if getattr(c, 'native_metadata_fault', None):
                    await inspect('native_metadata_' + c.native_metadata_fault)
            if lost_processing and not terminal_interrupt and lost_processing not in ('client-exit', 'partial-header', 'partial-body', 'read-eof') and not str(lost_processing).endswith('-connect') and not str(lost_processing).startswith('capacity-'):
                await inspect('lose_input_ack' if lost_processing == 'delivery-ack' else 'lose_processing')
            control = await mutate('control.claim', {'expectedRevision': initial_revision + 1, 'attachmentId': view['attachmentId']}, 'claim-native')
            matrix = await inspect('controls')
            assert matrix == {'refused': 20, 'noEffects': True}
            stream = c.launch.terminal_grants.record
            for unsupported in (b'/model other', b'/permissions', b'/logout', b'!true', b'\x1b[A',
                    b'\x1b[200~paste\x1b[201~', b'\t', b'line\n', b'\x03', 'snowman \u2603'.encode()):
                rejected = await call(client, 'terminal.input', {'attachmentId': view['attachmentId'],
                    'inputGrant': control['inputGrant'], 'inputEpoch': control['inputEpoch'], 'frameSeq': 1,
                    'data': base64.b64encode(unsupported).decode()})
                assert rejected['error']['data']['code'] == 'UNSUPPORTED', rejected
                assert stream.sequence == 0 and stream.processed == 0 and not stream.pending
                assert stream.private_draft == '' and not c.launch.peer.closed
            # Unsupported public mutations never reach native or reserve input.
            for method, fields in (('session.stop', {}), ('session.resume', {}), ('editor.open', {'path': 'fixture'}),
                    ('turn.steer', {}), ('config.set', {}), ('attachment.add', {})):
                rejected = await call(client, method, fields)
                assert 'error' in rejected, rejected
                assert stream.sequence == 0 and not stream.pending and not c.launch.peer.closed
            base_frame = {'attachmentId': view['attachmentId'], 'inputGrant': control['inputGrant'],
                'inputEpoch': control['inputEpoch'], 'frameSeq': 1, 'data': base64.b64encode(b'valid').decode()}
            for extra in ({'model': 'other'}, {'effort': 'high'}, {'attachments': ['unsupported']}):
                rejected = await call(client, 'terminal.input', {**base_frame, **extra})
                assert 'error' in rejected, rejected
                assert stream.sequence == 0 and not stream.pending and not c.launch.peer.closed
            rejected = await call(client, 'control.take', {'operationId': 'not-registered',
                'expectedRunId': str(c.launch.run_id), 'attachmentId': view['attachmentId'],
                'expectedRevision': control['inputEpoch'], 'reason': 'Unsupported L1 mutation'})
            assert 'error' in rejected, rejected
            assert stream.sequence == 0 and not stream.pending and not c.launch.peer.closed
            for method, fields in (('configuration.request', {'expectedRunId': str(c.launch.run_id),
                    'operationId': 'not-registered', 'expectedRevision': 0, 'model': 'other'}),
                    ('turn.submit', {'expectedRunId': str(c.launch.run_id), 'operationId': 'not-registered',
                    'text': 'rich', 'expectedConfigurationRevision': 0, 'expectedControlRevision': control['inputEpoch'],
                    'stagedIds': ['unsupported']})):
                rejected = await call(client, method, fields)
                assert 'error' in rejected, rejected
                assert stream.sequence == 0 and not stream.pending and not c.launch.peer.closed
            record_property('public_native_control_matrix', json.dumps({**matrix,
                'publicCodecRefusals': 10, 'publicUnsupportedMutations': 6, 'publicSchemaAndL1Refusals': 6, 'realCookie': True,
                'actualGrantedOwner': True, 'scope': 'Raw native observer controls plus actual granted HTTP frames; no browser UI'}))
            if getattr(c, 'native_exit_stage', None) == 'idle':
                await unexpected_codex_exit('idle', control=control)
                return
            params = {'attachmentId': view['attachmentId'], 'inputGrant': control['inputGrant'],
                'inputEpoch': control['inputEpoch'], 'frameSeq': 1,
                'data': base64.b64encode(b'Production Terminal input').decode()}
            if lost_processing in ('capacity-owner', 'capacity-global', 'capacity-native-socket'):
                from relay_capacity_helpers import qualify_public_capacity
                before = await inspect('state')
                if lost_processing == 'capacity-native-socket':
                    stats = await inspect('native_socket_capacity')
                else:
                    stats = await qualify_public_capacity(app, context.session_cookie, token, csrf, login,
                        call, SID, global_limit=lost_processing.endswith('global'), baseline=1)
                after = await inspect('state')
                assert before['requests'] == after['requests'] == 1
                reply = await call(client, 'terminal.input', params)
                assert reply['result']['watermark']['admitted'] == 1
                await asyncio.sleep(.7)
                assert 'Production Terminal input' in (await inspect('screen'))['text']
                await inspect('release_model')
                await c.manager.close()
                await inspect('finish')
                result = await task
                assert len(result['requests']) == 1
                assert result['terminalMediated'] is False
                record_property('native_public_capacity', json.dumps(stats))
                return
            if lost_processing in ('partial-header', 'partial-body', 'read-eof'):
                stream = c.launch.terminal_grants.record
                await inspect('transport_fault_' + lost_processing)
                reply = await call(client, 'terminal.input', params)
                assert 'error' in reply, reply
                assert c.launch.terminal_grants.record is None and stream.phase == 'revoked'
                assert stream.processed == 0 and stream.native is None
                with c.db() as db:
                    assert db.get(RelaySession, SID).sealed
                assert 'error' in await call(client, 'terminal.input', params)
                assert 'error' in await call(client, 'terminal.drain', {'attachmentId': view['attachmentId'],
                    'inputGrant': control['inputGrant']})
                await inspect('release_model')
                await c.manager.close()
                await inspect('finish')
                result = await task
                assert len(result['requests']) == 1
                assert result['terminalMediated'] is False
                record_property('native_partial_transport', f'{lost_processing}: actual owned socket frame cut/EOF, original stream poisoned/sealed, no submission or replay; installed stock native client and app-server')
                return
            if str(lost_processing).endswith('-connect'):
                await inspect('hold_connect')
                pending_frame = asyncio.create_task(call(client, 'terminal.input', params))
                try:
                    for _ in range(500):
                        waiting = await inspect('connect_state')
                        if waiting['waiting']:
                            break
                        await asyncio.sleep(.02)
                    assert waiting['waiting']
                    # The native opener await has completed, but no write passed
                    # the final current-grant check yet. Expiry must win here.
                    before = await inspect('screen')
                    assert 'Production Terminal input' not in before['text']
                    stream = c.launch.terminal_grants.record
                    if lost_processing == 'expired-connect':
                        stream.expires = time.monotonic() - 1
                    else:
                        @write_intent
                        def revoke():
                            with c.db() as db:
                                if lost_processing == 'stopped-connect':
                                    db.get(Session, SID).desired_state = 'stopped'
                                else:
                                    auth.end_session(db, token)
                                db.commit()
                        revoke()
                    await inspect('release_connect')
                    assert 'error' in await asyncio.wait_for(pending_frame, 10)
                finally:
                    await inspect('release_connect')
                    if not pending_frame.done():
                        pending_frame.cancel()
                    await asyncio.gather(pending_frame, return_exceptions=True)
                with c.db() as db:
                    assert db.get(RelaySession, SID).sealed
                assert c.launch.terminal_grants.record is None and stream.phase == 'revoked'
                assert stream.processed == 0 and stream.native is None
                assert (await inspect('connect_state'))['written'] == 0
                replay = await client.post(f'/api/sessions/{SID}/relay', json={
                    'jsonrpc': '2.0', 'id': 'refused-replay', 'method': 'ry/terminal.input',
                    'params': {'sessionId': SID, **params}})
                assert replay.status_code in (200, 401)
                assert replay.status_code == 401 or 'error' in replay.json()
                await inspect('release_model')
                await c.manager.close()
                await inspect('finish')
                result = await task
                assert len(result['requests']) == 1  # initial Chat only; ended authority never submits
                assert result['terminalMediated'] is False
                record_property('production_codex_scope', f'actual native opener await plus {lost_processing} before final IO admission; no native submission/replay; no browser UI')
                return
            assert 'result' in await call(client, 'terminal.input', params)
            before_enter = await call(client, 'terminal.drain', {'attachmentId': view['attachmentId'], 'inputGrant': control['inputGrant']})
            assert before_enter['result'] == {'epoch': control['inputEpoch'], 'admitted': 1, 'processed': 0}
            quiet_started = time.perf_counter()
            await asyncio.sleep(.6)  # stock TUI paste-burst boundary; no synthetic replay
            latency.append({'method': 'terminal-enter-quiet', 'quiet_ms': (time.perf_counter() - quiet_started) * 1000})
            enter_started = time.perf_counter()
            params = {**params, 'frameSeq': 2, 'data': base64.b64encode(b'\r').decode()}
            if lost_processing == 'client-exit':
                stream = c.launch.terminal_grants.record
                assert (await inspect('crash_view'))['exited']
                reply = await call(client, 'terminal.input', params)
                assert 'error' in reply, reply
                assert c.launch.terminal_grants.record is None and stream.phase == 'revoked'
                assert stream.processed == 0 and stream.native is None
                with c.db() as db:
                    assert db.get(RelaySession, SID).sealed
                assert 'error' in await call(client, 'terminal.input', params)
                assert 'error' in await call(client, 'terminal.drain', {
                    'attachmentId': view['attachmentId'], 'inputGrant': control['inputGrant']})
                await inspect('release_model')
                await c.manager.close()
                await inspect('finish')
                result = await task
                assert len(result['requests']) == 1  # prefix was never submitted or replayed
                assert result['terminalMediated'] is False
                record_property('production_codex_scope', 'kernel-confirmed stock TUI exit after draft delivery; next input refuses/fences without native submit or replay; app-server owner is distinct')
                return
            if lost_processing == 'delivery-ack':
                stream = c.launch.terminal_grants.record
                reply = await call(client, 'terminal.input', params)
                assert 'error' in reply  # delivery answer is unknown, never retried
                assert stream.processed == 2 and stream.native.operation_id
                with c.db() as db:
                    assert db.get(RelaySession, SID).sealed
                    operation = db.get(RelayOperation, stream.native.operation_id)
                    assert operation.receipt['state'] == 'accepted'
                    assert operation.receipt['actor']['principal'] == 'alex'
                assert 'error' in await call(client, 'terminal.input', params)
                assert 'error' in await call(client, 'terminal.drain', {
                    'attachmentId': view['attachmentId'], 'inputGrant': control['inputGrant']})
                await inspect('release_model')
                await c.manager.close()
                await inspect('finish')
                result = await task
                assert len(result['requests']) == 2  # actual accepted native input, no replay
                assert result['terminalMediated'] is False
                record_property('production_codex_scope', 'actual native processing before injected delivery ACK loss; accepted receipt retained, run fenced, no replay; no browser UI')
                return
            assert 'result' in await call(client, 'terminal.input', params)
            latency.append({'method': 'terminal-enter-admission', 'dispatch_ms': (time.perf_counter() - enter_started) * 1000})
            if lost_processing and not terminal_interrupt:
                for _ in range(500):
                    value = await inspect('state')
                    if value['processingLost'] and value['modelHeld']:
                        break
                    await asyncio.sleep(.02)
                assert value['processingLost'] and value['modelHeld'] and value['processed'] == 0
                stream = c.launch.terminal_grants.record
                assert stream.processed == 0
                drained = await call(client, 'terminal.drain', {'attachmentId': view['attachmentId'], 'inputGrant': control['inputGrant']})
                assert drained['result']['processed'] == 0  # accepted provider receipt is still not processing proof
                native_operation = stream.native.operation_id
                snap = (await call(client, 'session.snapshot', {}))['result']
                assert any(receipt['operationId'] == native_operation and receipt['state'] == 'accepted'
                    for receipt in snap['receipts'])  # provider admission is settled, processing is not
                await inspect('release_model')
                await idle()
                payload = {'sessionId': SID, 'expectedRunId': str(c.launch.run_id), 'view': 'chat',
                    'reason': 'Must not retire an unacknowledged native submission',
                    'expectedRevision': snap['controlRevision'], 'attachmentId': view['attachmentId']}
                registration = await call(client, 'operation.register', {'expectedRunId': str(c.launch.run_id),
                    'registrationKey': 'unknown-processing-take', 'method': 'writer.take',
                    'payloadDigest': p.operation_digest('writer.take', payload)})
                assert 'result' in registration, registration
                refused = await call(client, 'writer.take', {**payload, 'operationId': registration['result']['operationId']})
                assert 'error' in refused, refused
                assert 'error' in await call(client, 'terminal.input', {**params, 'frameSeq': 3})
                assert stream.processed == 0
                with c.db() as db:
                    assert db.get(RelaySession, SID).sealed
                await c.manager.close()
                await inspect('finish')
                result = await task
                assert len(result['requests']) == 2  # no client/Host replay after the lost acknowledgement
                assert result['terminalMediated'] is False
                record_property('production_codex_scope', 'actual native/main admission; injected processing acknowledgement loss; no processing/take/replay')
                return
            for _ in range(500):
                value = await inspect('state')
                if value['processed'] == 2 and (permission == 'fast' or value['modelHeld']) and value['active_turn'] is not None:
                    break
                await asyncio.sleep(.02)
            assert value['processed'] == 2 and (permission == 'fast' or value['modelHeld']) and value['active_turn'] is not None
            latency.append({'method': 'terminal-enter-processing', 'processing_ms': (time.perf_counter() - enter_started) * 1000})
            if terminal_interrupt:
                stream = c.launch.terminal_grants.record
                active = stream.native.operation_id
                snap = (await call(client, 'session.snapshot', {}))['result']
                assert snap['activeTurnId'] == active and snap['writer']['view'] == 'terminal'
                native_before = await inspect('clients')
                receipt = await mutate('turn.interrupt', {'turnId': active,
                    'expectedControlRevision': snap['controlRevision']}, 'terminal-interrupt')
                broker_lost = lost_processing.endswith('-broker-lost')
                assert receipt['state'] == ('uncertain' if broker_lost else 'accepted'), receipt
                await idle()
                native = await inspect('interrupt_state')
                assert len(native['interrupts']) == 1 and len(native['completions']) == 1, native
                assert native['interrupts'][0]['turnId'] == native['completions'][0]['id']
                assert native['completions'][0]['status'] == 'interrupted'
                assert await inspect('clients') == native_before
                assert c.launch.terminal_grants.record is stream and stream.processed == 2
                repeated = await call(client, 'turn.interrupt', {'sessionId': SID, 'expectedRunId': str(c.launch.run_id),
                    'turnId': active, 'expectedControlRevision': snap['controlRevision'], 'operationId': receipt['operationId']})
                assert repeated['result'] == receipt
                observed = (await call(client, 'operation.get', {'operationId': receipt['operationId']}))['result']
                assert observed['state'] == 'accepted'
                assert observed['actor'] == receipt['actor'] and observed['payloadDigest'] == receipt['payloadDigest']
                assert len((await inspect('interrupt_state'))['interrupts']) == 1
                record_property('native_terminal_interrupt', json.dumps({'initial': receipt['state'], 'observed': observed['state'],
                    'native': native, 'processed': stream.processed}))
                replacement_frame = {**params, 'frameSeq': 3, 'data': base64.b64encode(b'Fresh replacement Terminal input').decode()}
                assert 'result' in await call(client, 'terminal.input', replacement_frame)
                await asyncio.sleep(.6)
                assert 'result' in await call(client, 'terminal.input', {**replacement_frame, 'frameSeq': 4,
                    'data': base64.b64encode(b'\r').decode()})
                for _ in range(500):
                    state = await inspect('state')
                    if state['processed'] == 4 and state['active_turn'] is not None and state['active_turn'] != active:
                        break
                    await asyncio.sleep(.02)
                assert state['processed'] == 4 and state['active_turn'] is not None and state['active_turn'] != active
                replacement_id = state['active_turn']
                stale_params = {'sessionId': SID, 'expectedRunId': str(c.launch.run_id), 'turnId': active,
                    'expectedControlRevision': snap['controlRevision']}
                registered = await call(client, 'operation.register', {'expectedRunId': str(c.launch.run_id),
                    'registrationKey': 'old-terminal-target', 'method': 'turn.interrupt',
                    'payloadDigest': p.operation_digest('turn.interrupt', stale_params)})
                assert 'result' in registered, registered
                refused = await call(client, 'turn.interrupt', {**stale_params,
                    'operationId': registered['result']['operationId']})
                assert refused['error']['data']['code'] == 'STALE_AUTHORITY', refused
                retained = await call(client, 'turn.interrupt', {**stale_params, 'operationId': receipt['operationId']})
                assert retained['result'] == observed
                assert (await inspect('state'))['active_turn'] == replacement_id
                assert len((await inspect('interrupt_state'))['interrupts']) == 1
                await inspect('release_model')
                await idle()
                final_native = await inspect('interrupt_state')
                assert len(final_native['interrupts']) == 1 and len(final_native['completions']) == 2
                assert final_native['completions'][1]['status'] == 'completed'
                record_property('native_interrupt_replacement', json.dumps(final_native))
                await c.manager.close()
                await inspect('finish')
                result = await task
                assert len(result['requests']) == 3 and result['terminalMediated'] is False
                return
            if getattr(c, 'native_exit_stage', None) == 'busy':
                await unexpected_codex_exit('busy', control=control)
                return
            busy_clients = await inspect('clients')
            busy_snapshot = (await call(client, 'session.snapshot', {}))['result']
            await capture_screen('actual native permission pending; immediate model response' if permission == 'fast'
                else 'real native turn active; model response held')
            busy_payload = {'sessionId': SID, 'expectedRunId': str(c.launch.run_id), 'view': 'chat',
                'reason': 'Refuse take while actual native work is held', 'expectedRevision': busy_snapshot['controlRevision'],
                'attachmentId': view['attachmentId']}
            registered = await call(client, 'operation.register', {'expectedRunId': str(c.launch.run_id),
                'registrationKey': 'busy-take', 'method': 'writer.take',
                'payloadDigest': p.operation_digest('writer.take', busy_payload)})
            assert 'result' in registered, registered
            refused = await call(client, 'writer.take', {**busy_payload, 'operationId': registered['result']['operationId']})
            assert refused['error']['data']['code'] == 'BUSY', refused
            still = (await call(client, 'session.snapshot', {}))['result']
            assert still['controlRevision'] == busy_snapshot['controlRevision']
            assert still['writer']['view'] == 'terminal' and still['writer']['active'] is True
            assert await inspect('clients') == busy_clients  # no new client, owner or lease
            await inspect('release_model')
            if getattr(c, 'native_metadata_fault', None):
                for _ in range(500):
                    facts = await inspect('native_metadata_state')
                    if facts['changes'] == 1:
                        break
                    await asyncio.sleep(.01)
                assert facts['changes'] == 1 and facts['requests'] == 2 and facts['answers'] == [] and not facts['marker']
                with c.db() as db:
                    from acc.server.models import RelayDecision
                    assert not db.scalars(select(RelayDecision).where(RelayDecision.session_id == SID)).all()
                blocked = await call(client, 'operation.register', {'expectedRunId': str(c.launch.run_id),
                    'registrationKey': 'metadata-refused-take', 'method': 'writer.take',
                    'payloadDigest': p.operation_digest('writer.take', busy_payload)})
                if 'result' in blocked:
                    reply = await call(client, 'writer.take', {**busy_payload, 'operationId': blocked['result']['operationId']})
                    assert 'error' in reply, reply
                    with c.db() as db:
                        assert db.get(RelayOperation, blocked['result']['operationId']).receipt['state'] != 'accepted'
                else:
                    assert 'error' in blocked
                assert 'error' in await call(client, 'terminal.input', {**params, 'frameSeq': 3})
                await c.manager.close()
                for _ in range(500):
                    cleanup = await inspect('cleanup_state')
                    if cleanup['active'] == 0:
                        break
                    await asyncio.sleep(.01)
                assert cleanup['active'] == 0 and cleanup['producer'] and all(cleanup['views'])
                facts = await inspect('native_metadata_state')
                assert facts['changes'] == 1 and facts['requests'] == 2 and facts['answers'] == [] and not facts['marker']
                await inspect('finish')
                result = await task
                assert len(result['requests']) == 2 and result['terminalMediated'] is False
                record_property('native_source_metadata', f'{c.native_metadata_fault}: alter actual native approval metadata before RPC decode; no decision/answer/effect or take/input/replay; cleanup verified')
                return
            if permission:
                for _ in range(500):
                    snap = (await call(client, 'session.snapshot', {}))['result']
                    if snap['pendingDecisions']:
                        break
                    await asyncio.sleep(.02)
                assert snap['pendingDecisions'], await inspect('permission_state')
                decision = snap['pendingDecisions'][0]
                assert decision['status'] == 'pending'
                if getattr(c, 'native_exit_stage', None) == 'pending':
                    await unexpected_codex_exit('pending', decision=decision, control=control)
                    return
                assert 'Command:\n/bin/sh' in decision['description'] and 'relay-permission-forbidden' in decision['description']
                assert 'Reason:\nOffline fixture requires a person decision' in decision['description']
                assert not (await inspect('permission_state'))['marker_exists']
                await capture_screen('actual native permission pending; hub decision owns the answer')
                if getattr(c, 'native_decision_fault', None) is not None:
                    from relay_decision_faults import change_decision_scope
                    payload = {'sessionId': SID, 'expectedRunId': str(c.launch.run_id),
                        'decisionId': decision['decisionId'], 'choiceId': 'accept',
                        'expectedRevision': decision['revision'], 'actionDigest': decision['actionDigest']}
                    registration = await call(client, 'operation.register', {'expectedRunId': str(c.launch.run_id),
                        'registrationKey': 'codex-await-allow', 'method': 'decision.reply',
                        'payloadDigest': p.operation_digest('decision.reply', payload)})
                    assert 'result' in registration, registration
                    if getattr(c, 'native_queue_decision', False):
                        await inspect('hold_approval_write')
                        original_handle = c.launch.peer.handle
                        checks = 0
                        async def count_reply_checks(method, values):
                            nonlocal checks
                            if method == 'authority.verify':
                                checks += 1
                            return await original_handle(method, values)
                        c.launch.peer.handle = count_reply_checks
                        pending = asyncio.create_task(client.post(f'/api/sessions/{SID}/relay', json={
                            'jsonrpc': '2.0', 'id': 'queued-codex-allow', 'method': 'ry/decision.reply',
                            'params': {**payload, 'operationId': registration['result']['operationId']}}))
                        try:
                            for _ in range(500):
                                waiting = await inspect('approval_write_state')
                                if waiting['waiting']:
                                    break
                                await asyncio.sleep(.01)
                            assert waiting == {'waiting': True, 'writes': 0}
                            before_checks = checks
                            change_decision_scope(c, SID, decision['decisionId'], c.native_decision_fault)
                            await inspect('release_approval_write')
                            await pending
                            assert checks > before_checks, 'Authority must be rechecked inside the actual write lock'
                            assert (await inspect('approval_write_state'))['writes'] == 0
                            assert not (await inspect('permission_state'))['marker_exists']
                        finally:
                            c.launch.peer.handle = original_handle
                            await inspect('release_approval_write')
                            await asyncio.gather(pending, return_exceptions=True)
                        await c.manager.close()
                        await inspect('finish')
                        result = await task
                        assert len(result['requests']) == 2
                        record_property('native_queued_decision_scope', c.native_decision_fault + ': actual approval reply blocked in native stdio write-lock after authorization; fresh scope checked before bytes, no reply/effect/replay')
                        return
                    original = c.launch.peer.handle
                    seen = False
                    async def change_after_verify(method, params):
                        nonlocal seen
                        result = await original(method, params)
                        if method == 'authority.verify' and not seen:
                            seen = True
                            change_decision_scope(c, SID, decision['decisionId'], c.native_decision_fault)
                        return result
                    c.launch.peer.handle = change_after_verify
                    try:
                        response = await client.post(f'/api/sessions/{SID}/relay', json={
                            'jsonrpc': '2.0', 'id': 'codex-await-allow', 'method': 'ry/decision.reply',
                            'params': {**payload, 'operationId': registration['result']['operationId']}})
                        assert response.status_code in (200, 401, 403, 409), response.text
                    finally:
                        c.launch.peer.handle = original
                    assert seen and c.native_scope_changed == c.native_decision_fault
                    permission_state = await inspect('permission_state')
                    assert not permission_state['marker_exists'] and permission_state['answers'] == []
                    with c.db() as db:
                        receipt = db.get(RelayOperation, registration['result']['operationId']).receipt
                        assert receipt['state'] != 'accepted' and receipt['actor']['principal'] == 'alex'
                    await c.manager.close()
                    await inspect('finish')
                    result = await task
                    assert len(result['requests']) == 2
                    assert result['terminalMediated'] is False
                    record_property('native_codex_decision_scope', c.native_decision_fault + ': changed after verified main response; final serialized admission refuses before native reply, original actor/op preserved, no effect/answer/replay')
                    return
                decline = next(choice['id'] for choice in decision['choices'] if choice['id'] == 'cancel')
                assert next(choice['label'] for choice in decision['choices'] if choice['id'] == 'cancel') == 'Cancel turn'
                assert {choice['id'] for choice in decision['choices']} == {'accept', 'cancel'}
                answer_payload = {'decisionId': decision['decisionId'], 'choiceId': decline,
                    'expectedRevision': decision['revision'], 'actionDigest': decision['actionDigest']}
                if permission == 'rollback':
                    context.settings = replace(context.settings, feature_overrides={'session_relay': False})
                    allow_payload = {'sessionId': SID, 'expectedRunId': str(c.launch.run_id), **answer_payload, 'choiceId': 'accept'}
                    refused = await call(client, 'operation.register', {'expectedRunId': str(c.launch.run_id),
                        'registrationKey': 'rollback-must-not-allow', 'method': 'decision.reply',
                        'payloadDigest': p.operation_digest('decision.reply', allow_payload)})
                    assert 'error' in refused, refused
                answer = await mutate('decision.reply', answer_payload, 'native-deny')
                assert answer['state'] == 'uncertain'  # native response write alone is not conclusive application
                await idle()
                replay = await call(client, 'decision.reply', {'sessionId': SID, 'expectedRunId': str(c.launch.run_id),
                    **answer_payload, 'operationId': answer['operationId']})
                assert replay['result'] == {**answer, 'state': 'accepted', 'revision': answer['revision'] + 1}
                state = await inspect('permission_state')
                assert not state['marker_exists'] and state['answers'] == [{'id': 0, 'decision': 'cancel'}]
                final = (await call(client, 'session.snapshot', {}))['result']
                outcome = next(value for value in final['decisionOutcomes'] if value['decisionId'] == answer_payload['decisionId'])
                assert outcome['status'] == 'denied' and outcome['choiceId'] == answer_payload['choiceId']
                stream = c.launch.terminal_grants.record
                assert stream.processed == 2
                await c.manager.close()
                await inspect('finish')
                result = await task
                assert len(result['requests']) == 2
                record_property('native_screens', json.dumps(native_screens))
                record_property('production_codex_scope', 'actual native tool approval through production hub; explicit person cancel; uncertain decision receipt stays truthful')
                record_property('native_permission_profile', 'immediate response, no model hold' if permission == 'fast' else str(permission))
                return
            for _ in range(500):
                value = await idle()
                if value['processed'] == 2:
                    break
                await asyncio.sleep(.02)
            if value['processed'] != 2:
                screen = await inspect('screen')
                if 'Failed to resume session' in screen['text']:
                    raise AssertionError('Pinned production cold native history bootstrap failed; input was not processed')
                raise AssertionError(value)
            drained = await call(client, 'terminal.drain', {'attachmentId': view['attachmentId'], 'inputGrant': control['inputGrant']})
            assert drained['result'] == {'epoch': control['inputEpoch'], 'admitted': 2, 'processed': 2}
            clients_before = await inspect('clients')
            params = {**params, 'frameSeq': 3, 'data': base64.b64encode(b'Retain this unsent native draft').decode()}
            assert 'result' in await call(client, 'terminal.input', params)
            await asyncio.sleep(.6)
            assert 'Retain this unsent native draft' in (await inspect('screen'))['text']
            snap = (await call(client, 'session.snapshot', {}))['result']
            await mutate('writer.take', {'view': 'chat', 'reason': 'Person returns to Chat',
                'expectedRevision': snap['controlRevision'], 'attachmentId': view['attachmentId']}, 'to-chat')
            assert 'error' in await call(client, 'terminal.input', {**params, 'frameSeq': 4})
            snap = (await call(client, 'session.snapshot', {}))['result']
            await mutate('turn.submit', {'text': 'Chat after production take',
                'expectedControlRevision': snap['controlRevision'], 'expectedConfigurationRevision': 0, 'ifBusy': 'reject'}, 'last-chat')
            await idle()
            clients_after = await inspect('clients')
            assert clients_before['nativeId'] == clients_after['nativeId']
            assert clients_before['owner'] == clients_after['owner']
            old, fresh = clients_after['views'][-2:]
            assert old['retired'] is True and old['draft'] == 'Retain this unsent native draft'
            assert old['admitted'] == 3 and old['processed'] == 2  # retained is not processed
            assert fresh['draft'] == '' and fresh['fresh'] is True
            assert old['name'] != fresh['name'] and old['peer'] != fresh['peer']
            for _ in range(100):
                response = await client.get(f'/api/sessions/{SID}/relay/terminal', params={
                    'attachmentId': view['attachmentId'], 'expectedRunId': str(c.launch.run_id)})
                assert response.status_code == 200, response.text
                text = response.json()['text']
                if last_chat_response in text and 'Retain this unsent native draft' in text:
                    break
                await asyncio.sleep(.05)
            assert 'Permanently read-only previous client' in text
            assert last_chat_response in text and 'Retain this unsent native draft' in text
            native_screens.append({'phase': 'retired draft still visible after fresh Chat work', 'text': text})
            assert snap['terminalMediated'] is False
            @write_intent
            def second_login():
                with c.db() as db:
                    person = db.get(User, 2)
                    person.role = 'owner'  # artificial authorized second person, never impersonate the first
                    credentials = auth.create_session(db, person, 'second-fixture-person')
                    db.commit()
                    return credentials
            other_token, other_csrf = second_login()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://fixture',
                headers={'Origin': 'http://fixture', 'X-ACC-CSRF': other_csrf},
                cookies={context.session_cookie: other_token}) as other:
                other_view = await attach(other, 'second-native-person', ['writer', 'terminal'])
                before = (await call(other, 'session.snapshot', {}))['result']
                assert before['writer']['mine'] is False
                takeover = await mutate('writer.take', {'view': 'terminal', 'reason': 'Second person takes fresh Terminal input',
                    'expectedRevision': before['controlRevision'], 'attachmentId': other_view['attachmentId']}, 'fresh-terminal', using=other)
                assert takeover['actor']['principal'] == 'sam'
                current = (await call(other, 'session.snapshot', {}))['result']
                assert current['writer']['mine'] is True and current['writer']['holder']['principal'] == 'sam'
                assert (await call(client, 'session.snapshot', {}))['result']['writer']['mine'] is False
                control = await mutate('control.claim', {'expectedRevision': current['controlRevision'],
                    'attachmentId': other_view['attachmentId']}, 'fresh-claim', using=other)
                replacement = {'attachmentId': other_view['attachmentId'], 'inputGrant': control['inputGrant'],
                    'inputEpoch': control['inputEpoch'], 'frameSeq': 1,
                    'data': base64.b64encode(b'Fresh replacement Terminal input').decode()}
                # Copying another person's opaque grant/attachment cannot grant this first connection input.
                assert 'error' in await call(client, 'terminal.input', replacement)
                # Match the browser's append-at-end keyboard profile: individual
                # characters, a real edit, then Enter across the native quiet boundary.
                keyboard = b'Fresh replacement Terminal inpuX\x7ft'
                for sequence, byte in enumerate(keyboard, 1):
                    reply = await call(other, 'terminal.input', {**replacement, 'frameSeq': sequence,
                        'data': base64.b64encode(bytes([byte])).decode()})
                    assert reply['result']['watermark']['admitted'] == sequence
                    assert reply['result']['watermark']['processed'] == 0
                await asyncio.sleep(.6)
                submitted_sequence = len(keyboard) + 1
                assert 'result' in await call(other, 'terminal.input', {**replacement, 'frameSeq': submitted_sequence,
                    'data': base64.b64encode(b'\r').decode()})
                for _ in range(500):
                    state = await idle()
                    if state['processed'] == submitted_sequence:
                        break
                    await asyncio.sleep(.02)
                assert state['processed'] == submitted_sequence
                assert (await call(other, 'terminal.drain', {'attachmentId': other_view['attachmentId'],
                    'inputGrant': control['inputGrant']}))['result'] == {
                        'epoch': control['inputEpoch'], 'admitted': submitted_sequence, 'processed': submitted_sequence}
                operation = c.launch.terminal_grants.record.native.operation_id
                with c.db() as db:
                    native_receipt = db.get(RelayOperation, operation).receipt
                    assert native_receipt['actor']['principal'] == 'sam' and native_receipt['state'] == 'accepted'
                    audit = db.scalar(select(AuditLog).where(AuditLog.target == SID,
                        AuditLog.action == 'relay.writer.take', AuditLog.actor == 'sam'))
                    assert audit is not None and audit.detail['channel'] == 'chat'
                    assert audit.detail['previous']['principal'] == 'alex' and audit.detail['previous']['channel'] == 'chat'
                    assert audit.detail['view'] == 'terminal' and audit.detail['control_revision'] == current['controlRevision']
                assert 'error' in await call(client, 'terminal.input', {**params, 'frameSeq': 4})
                for reader, attachment in ((client, view), (other, other_view)):
                    response = await reader.get(f'/api/sessions/{SID}/relay/terminal', params={
                        'attachmentId': attachment['attachmentId'], 'expectedRunId': str(c.launch.run_id)})
                    assert response.status_code == 200, response.text
                    assert 'Retain this unsent native draft' in response.json()['text']
        if not cold_first and not lost_processing and not permission:
            from acc.server.session_relay.hub import seal_for_stop
            from acc.server.session_relay.runtime import RuntimeManager
            @write_intent
            def stop_intent():
                seal_for_stop(c.ctx, SID)
                with c.db() as db:
                    db.get(Session, SID).desired_state = 'stopped'
                    db.commit()
            stop_intent()
            helper_loss = getattr(c, 'native_stop_helper_loss', False)
            if helper_loss:
                assert (await inspect('crash_producer_supervisor'))['root_returncode'] == -9
            assert await c.manager.stop(SID) is (not helper_loss)
            retired_path = output / 'retired-stop.json'
            retired = json.loads(retired_path.read_text()) if retired_path.exists() else None
            if not helper_loss:
                assert retired['result'] == {'exited': True, 'wholeTree': 'enclosure-v1'}
                assert retired['producer'] is True and retired['active'] == 0
                assert len(retired['views']) >= 2 and all(value is True for value in retired['views'])
            fresh = RuntimeManager(context)
            try:
                assert await c.manager.stop(SID) is (not helper_loss)
                assert await fresh.stop(SID) is (not helper_loss)
                assert not fresh.runs and not fresh.launches
            finally:
                await fresh.close()
            record_property('retired_stop', json.dumps(retired))
        await c.manager.close()
        await inspect('finish')
        result = await task
        assert len(result['requests']) == (3 if cold_first else 4)
        assert all('Retain this unsent native draft' not in request['input_markers'] for request in result['requests'])
        assert result['requests'][-1]['input_markers'][-1] == 'Fresh replacement Terminal input'
        assert result['terminalMediated'] is False
        record_property('native_screens', json.dumps(native_screens))
        record_property('production_codex_scope', 'actual broker/main/stock TUI/public cookie grants and take; fixture capability overrides, no browser UI')
    except Exception as error:
        if output is not None and (output / 'inspect.sock').exists() and not task.done():
            try:
                error.add_note(json.dumps(await inspect('screen')))
            except Exception:
                pass
        await asyncio.sleep(.05)
        if output is not None and (output / 'native-error.log').exists():
            error.add_note((output / 'native-error.log').read_text()[-8000:])
        if output is not None and (output / 'preclose-screen.jsonl').exists():
            error.add_note((output / 'preclose-screen.jsonl').read_text()[-12000:])
        if output is not None and (output / 'errors.log').exists():
            error.add_note((output / 'errors.log').read_text())
        raise
    finally:
        record_property('relay_latency', json.dumps(latency))
        await c.manager.close()
        if output is not None and (output / 'inspect.sock').exists() and not task.done():
            await inspect('finish')
        if brokers:
            await brokers.close()
        await asyncio.gather(task, return_exceptions=True)
        if not task.cancelled() and task.exception() is None:
            record_property('relay_native_spans', json.dumps(task.result().get('timings', [])))


async def test_native_codex_supervisor_loss_never_becomes_retired_stop(runtime_case, monkeypatch, record_property):  # noqa: F811
    # The outer disposable PID namespace owns final cleanup of this deliberate
    # supervisor loss; no real account or out-of-namespace process is involved.
    runtime_case.native_stop_helper_loss = True
    await test_production_codex_native_public_take_and_input(
        runtime_case, monkeypatch, record_property, False, False, False)


async def test_native_codex_cookie_loss_after_dispatch_verification(runtime_case, monkeypatch, record_property):  # noqa: F811
    runtime_case.native_submit_revoke = True
    await test_production_codex_native_public_take_and_input(
        runtime_case, monkeypatch, record_property, False, False, False)
