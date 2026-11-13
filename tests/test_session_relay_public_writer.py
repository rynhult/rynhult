"""Public writer contract over real main/private broker; artificial qualification only."""
import asyncio

import httpx
import pytest
from fastapi import FastAPI, Request
from test_session_relay_runtime import runtime_case  # noqa: F401
from test_session_relay_writer_routes import retire

from acc.server.api.session_relay import router
from acc.server.db import write_intent
from acc.server.models import RelayOperation, RelayOwnership, RelaySession
from acc.session_relay import protocol as p

SID = 'abc123def456'


@pytest.fixture
async def public_case(runtime_case, monkeypatch):  # noqa: F811
    case = runtime_case
    await case.launch.launch(SID)
    case.ctx.relay_runtime = case.manager
    def principal(request: Request):
        request.state.cookie_authenticated = True
        request.state.session_hash = 'fixture-login'
        return case.resolve()[0]
    case.ctx.principal = principal
    case.ctx.relay_principal = lambda _db, request: principal(request)
    app = FastAPI()
    app.include_router(router(case.ctx))
    case.app = app
    case.broker_state = next(iter(case.factory.active.values()))
    case.broker_state.writers.retire = retire  # synthetic retirement; no native Host qualification
    @write_intent
    def qualify_fixture():
        with case.db() as db:
            db.get(RelaySession, SID).capabilities = {'text': 'qualified', 'terminal.input': 'qualified'}
            db.commit()
    case.qualify_fixture = qualify_fixture
    return case


async def call(client, method, params):
    response = await client.post(f'/api/sessions/{SID}/relay', json={
        'jsonrpc': '2.0', 'id': 'fixture-request', 'method': 'ry/' + method,
        'params': params if method == 'initialize' else {'sessionId': SID, **params}})
    assert response.status_code == 200, response.text
    return response.json()


async def attach(client, identifier, features=None):
    value = (await call(client, 'initialize', {'major': 1, 'minor': 0, 'features': features or ['writer'], 'clientId': identifier}))['result']
    client.headers['X-ACC-Relay-Connection'] = value['connectionToken']
    return (await call(client, 'session.attach', {'clientId': identifier, 'connectionSeq': 1}))['result']


async def register(client, case, params, key):
    return await call(client, 'operation.register', {'expectedRunId': str(case.launch.run_id),
        'registrationKey': key, 'method': 'writer.take',
        'payloadDigest': p.operation_digest('writer.take', {'sessionId': SID, **params})})


async def test_public_writer_stays_unavailable_despite_fabricated_capabilities(public_case):
    case = public_case
    case.qualify_fixture()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(case.app), base_url='http://fixture') as client:
        view = await attach(client, 'default-gate')
        assert view['snapshot']['terminalMediated'] is False
        assert view['snapshot']['writer']['available'] is False
        params = {'expectedRunId': str(case.launch.run_id), 'attachmentId': view['attachmentId'],
                  'expectedRevision': 1, 'view': 'terminal', 'reason': 'Artificial take'}
        assert (await register(client, case, params, 'blocked'))['error']['data']['code'] == 'UNSUPPORTED'
    assert case.broker_state.writers.gate.epoch == 1


async def test_public_take_returns_receipt_not_fake_input_grant_and_binds_attachment(public_case, monkeypatch):
    case = public_case
    case.qualify_fixture()
    monkeypatch.setattr('acc.server.session_relay.writers.PUBLIC_WRITER_QUALIFIED', True)
    transport = httpx.ASGITransport(case.app)
    async with httpx.AsyncClient(transport=transport, base_url='http://fixture') as first, httpx.AsyncClient(transport=transport, base_url='http://fixture') as observer:
        view = await attach(first, 'person-take')
        other = await attach(observer, 'observer')
        params = {'expectedRunId': str(case.launch.run_id), 'attachmentId': view['attachmentId'],
                  'expectedRevision': 1, 'view': 'terminal', 'reason': 'Artificial person take'}
        registration = (await register(first, case, params, 'first'))['result']
        result = await call(first, 'writer.take', {**params, 'operationId': registration['operationId']})
        receipt = result['result']
        assert receipt['state'] == 'accepted' and receipt['method'] == 'writer.take'
        assert not {'inputGrant', 'inputEpoch'} & receipt.keys()
        assert (await call(first, 'session.snapshot', {}))['result']['writer']['mine'] is True
        assert (await call(observer, 'session.snapshot', {}))['result']['writer']['mine'] is False
        assert (await call(observer, 'writer.take', {**params, 'operationId': registration['operationId']}))['error']
        with case.db() as db:
            owner = db.get(RelayOwnership, SID)
            assert owner.authority['writerAttachment'] == view['attachmentId']
        assert case.broker_state.writers.gate.epoch == 2
        assert other['attachmentId'] != view['attachmentId']


async def test_detached_attachment_during_take_never_commits_new_writer(public_case, monkeypatch):
    case = public_case
    case.qualify_fixture()
    monkeypatch.setattr('acc.server.session_relay.writers.PUBLIC_WRITER_QUALIFIED', True)
    reached, release = asyncio.Event(), asyncio.Event()
    async def blocked_retire(lease):
        reached.set()
        await release.wait()
        return await retire(lease)
    case.broker_state.writers.retire = blocked_retire
    async with httpx.AsyncClient(transport=httpx.ASGITransport(case.app), base_url='http://fixture') as client:
        view = await attach(client, 'revoked')
        params = {'expectedRunId': str(case.launch.run_id), 'attachmentId': view['attachmentId'],
                  'expectedRevision': 1, 'view': 'terminal', 'reason': 'Artificial take during disconnect'}
        operation = (await register(client, case, params, 'during-await'))['result']['operationId']
        pending = asyncio.create_task(call(client, 'writer.take', {**params, 'operationId': operation}))
        await asyncio.wait_for(reached.wait(), 5)
        await call(client, 'session.detach', {'attachmentId': view['attachmentId'], 'connectionSeq': 2})
        release.set()
        assert 'error' in await asyncio.wait_for(pending, 5)
    with case.db() as db:
        assert db.get(RelayOwnership, SID).view == 'chat'
        assert db.get(RelaySession, SID).sealed
        assert db.get(RelayOperation, operation).receipt['state'] == 'uncertain'


async def test_same_person_other_tab_cannot_send_on_public_chat_lease(public_case, monkeypatch):
    case = public_case
    case.qualify_fixture()
    monkeypatch.setattr('acc.server.session_relay.writers.PUBLIC_WRITER_QUALIFIED', True)
    transport = httpx.ASGITransport(case.app)
    async with httpx.AsyncClient(transport=transport, base_url='http://fixture') as holder, httpx.AsyncClient(transport=transport, base_url='http://fixture') as other:
        view = await attach(holder, 'holder-chat')
        await attach(other, 'other-chat')
        take = {'expectedRunId': str(case.launch.run_id), 'attachmentId': view['attachmentId'],
                'expectedRevision': 1, 'view': 'chat', 'reason': 'Artificial explicit Chat input'}
        identifier = (await register(holder, case, take, 'take-chat'))['result']['operationId']
        assert (await call(holder, 'writer.take', {**take, 'operationId': identifier}))['result']['state'] == 'accepted'
        params = {'sessionId': SID, 'expectedRunId': str(case.launch.run_id), 'text': 'Other attachment must not send',
                  'expectedConfigurationRevision': 0, 'expectedControlRevision': 2}
        registration = (await call(other, 'operation.register', {'expectedRunId': str(case.launch.run_id),
            'registrationKey': 'other-send', 'method': 'turn.submit', 'payloadDigest': p.operation_digest('turn.submit', params)}))['result']
        reply = await call(other, 'turn.submit', {**params, 'operationId': registration['operationId']})
        assert reply['error']['data']['code'] == 'STALE_AUTHORITY'
    assert not any(method == 'turn/start' for method, _ in case.owners[0].calls)


async def test_initial_public_chat_is_observer_until_explicit_attachment_take(public_case, monkeypatch):
    case = public_case
    case.qualify_fixture()
    monkeypatch.setattr('acc.server.session_relay.writers.PUBLIC_WRITER_QUALIFIED', True)
    transport = httpx.ASGITransport(case.app)
    async with httpx.AsyncClient(transport=transport, base_url='http://fixture') as first, httpx.AsyncClient(transport=transport, base_url='http://fixture') as other:
        view = await attach(first, 'initial-chat')
        await attach(other, 'initial-observer')
        assert view['snapshot']['writer']['mine'] is False
        for client in (first, other):
            assert (await call(client, 'session.snapshot', {}))['result']['writer']['mine'] is False
            params = {'sessionId': SID, 'expectedRunId': str(case.launch.run_id),
                'text': 'Unclaimed initial Chat must not send', 'expectedConfigurationRevision': 0,
                'expectedControlRevision': 1}
            registered = (await call(client, 'operation.register', {'expectedRunId': str(case.launch.run_id),
                'registrationKey': 'initial-send', 'method': 'turn.submit',
                'payloadDigest': p.operation_digest('turn.submit', params)}))['result']
            denied = await call(client, 'turn.submit', {**params, 'operationId': registered['operationId']})
            assert denied['error']['data']['code'] == 'STALE_AUTHORITY'
    assert not any(method == 'turn/start' for method, _ in case.owners[0].calls)


async def test_pending_turn_take_refusal_keeps_receipt_and_audit_writable(public_case, monkeypatch):
    from sqlalchemy import select

    from acc.server.models import AuditLog
    from acc.server.session_relay.hub import Hub

    case = public_case
    case.qualify_fixture()
    monkeypatch.setattr('acc.server.session_relay.writers.PUBLIC_WRITER_QUALIFIED', True)
    pending = Hub(case.ctx).register(case.resolve, SID, str(case.launch.run_id), 'pending-delivery',
        'turn.submit', p.sha256_digest(b'pending-delivery'))
    for number in range(8):
        Hub(case.ctx).register(case.resolve, SID, str(case.launch.run_id), f'other-pending-{number}',
            'turn.submit', p.sha256_digest(f'pending-{number}'.encode()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(case.app), base_url='http://fixture') as client:
        view = await attach(client, 'pending-take')
        params = {'expectedRunId': str(case.launch.run_id), 'attachmentId': view['attachmentId'],
                  'expectedRevision': 1, 'view': 'terminal', 'reason': 'Take races queued work'}
        operation = (await register(client, case, params, 'pending-take'))['result']['operationId']
        result = await call(client, 'writer.take', {**params, 'operationId': operation})
        assert result['error']['data']['code'] == 'BUSY'
        # A second durable registration after refusal must not inherit a reader
        # snapshot retained by the exception/cursor and fail SQLITE_BUSY_SNAPSHOT.
        next_operation = await register(client, case, params, 'after-refusal')
        assert 'result' in next_operation, next_operation
    with case.db() as db:
        assert db.get(RelayOperation, pending.operation_id).receipt['state'] == 'queued'
        assert db.get(RelayOperation, operation).receipt['state'] == 'failed'
        audits = db.scalars(select(AuditLog).where(AuditLog.action == 'relay.refused')).all()
        assert any(row.detail.get('method') == 'writer.take' and row.detail.get('code') == 'BUSY' for row in audits)
        assert db.get(RelayOwnership, SID).phase == 'owned'
        assert db.get(RelaySession, SID).control_revision == 1
    assert case.broker_state.writers.gate.epoch == 1
    assert not any(method == 'turn/start' for method, _ in case.owners[0].calls)


async def test_competing_registered_takes_require_explicit_cancel_and_never_replay(public_case, monkeypatch):
    from sqlalchemy import select

    from acc.server.models import AuditLog

    case = public_case
    case.qualify_fixture()
    monkeypatch.setattr('acc.server.session_relay.writers.PUBLIC_WRITER_QUALIFIED', True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(case.app), base_url='http://fixture') as client:
        view = await attach(client, 'competing-takes')
        params = {'expectedRunId': str(case.launch.run_id), 'attachmentId': view['attachmentId'],
                  'expectedRevision': 1, 'view': 'terminal', 'reason': 'Explicit writer change'}
        first = (await register(client, case, params, 'competing-first'))['result']
        second = (await register(client, case, params, 'competing-second'))['result']
        refused = await call(client, 'writer.take', {**params, 'operationId': first['operationId']})
        assert refused['error']['data']['code'] == 'BUSY'
        # The refusal settles only its own take. Cancel the remaining request
        # explicitly, then obtain a newly registered authority-bound operation.
        cancelled = await call(client, 'operation.cancel', {'operationId': second['operationId'],
            'expectedRevision': second['receipt']['revision']})
        assert cancelled['result']['state'] == 'failed' and cancelled['result']['reason'] == 'cancelled'
        third = (await register(client, case, params, 'competing-third'))['result']
        accepted = await call(client, 'writer.take', {**params, 'operationId': third['operationId']})
        assert accepted['result']['state'] == 'accepted'
        for operation in (first, second):
            replay = await call(client, 'writer.take', {**params, 'operationId': operation['operationId']})
            assert 'error' in replay or replay['result']['state'] == 'failed'
        # Observe the accepted receipt again without a second provider transfer.
        repeated = await call(client, 'writer.take', {**params, 'operationId': third['operationId']})
        assert repeated['result'] == accepted['result']
    with case.db() as db:
        audits = db.scalars(select(AuditLog).where(AuditLog.action == 'relay.writer.take')).all()
        assert len(audits) == 1
        assert audits[0].actor == 'alex' and audits[0].detail['channel'] == 'chat'
        assert audits[0].detail['previous']['principal'] == 'alex'
        assert audits[0].detail['previous']['channel'] == 'chat'
        assert db.get(RelaySession, SID).control_revision == 2
    assert case.broker_state.writers.gate.epoch == 2


async def test_person_shaped_service_cannot_cancel_queued_person_operation(public_case):
    case = public_case
    async with httpx.AsyncClient(transport=httpx.ASGITransport(case.app), base_url='http://fixture') as client:
        await attach(client, 'person-before-service')
        # Registration remains available with public writer qualification off.
        from acc.server.session_relay.hub import Hub
        operation = Hub(case.ctx).register(case.resolve, SID, str(case.launch.run_id), 'person-cancel-scope',
            'turn.submit', p.sha256_digest(b'person-only'))
        def service_principal(request):
            request.state.cookie_authenticated = False
            return case.resolve()[0]  # same user_id/name is still not a human login
        case.ctx.principal = service_principal
        case.ctx.relay_principal = lambda _db, request: service_principal(request)
        client.headers.pop('X-ACC-Relay-Connection')
        await attach(client, 'service-observer')
        refused = await call(client, 'operation.cancel', {'operationId': operation.operation_id,
            'expectedRevision': operation.receipt.revision})
        assert refused['error']['data']['code'] == 'NOT_FOUND_OR_DENIED'
    with case.db() as db:
        assert db.get(RelayOperation, operation.operation_id).receipt == p.to_wire(operation.receipt)
        assert db.get(RelaySession, SID).active_operation_id is None
    assert case.broker_state.writers.gate.epoch == 1
    assert not any(method == 'turn/start' for method, _ in case.owners[0].calls)


async def test_public_cancel_reclaims_queued_payload_while_active_turn_keeps_running(public_case, monkeypatch):
    from dataclasses import replace

    case = public_case
    case.qualify_fixture()
    monkeypatch.setattr('acc.server.session_relay.writers.PUBLIC_WRITER_QUALIFIED', True)
    case.broker_state.adapter.record = replace(case.broker_state.adapter.record,
        qualification_id='fixture-only', qualification_date='2026-11-12',
        operations={'text': p.CapabilityEntry(p.Support.QUALIFIED, evidence=('test fake only',))})
    @write_intent
    def qualify_queue_fixture():
        with case.db() as db:
            row = db.get(RelaySession, SID)
            row.capabilities = {**row.capabilities, 'queue': 'qualified'}
            db.commit()
    qualify_queue_fixture()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(case.app), base_url='http://fixture') as client:
        view = await attach(client, 'cancel-private-queue', ['writer', 'queue'])
        params = {'expectedRunId': str(case.launch.run_id), 'attachmentId': view['attachmentId'],
                  'expectedRevision': 1, 'view': 'chat', 'reason': 'Use one existing Chat writer'}
        take = (await register(client, case, params, 'cancel-queue-take'))['result']
        assert (await call(client, 'writer.take', {**params, 'operationId': take['operationId']}))['result']['state'] == 'accepted'
        async def submit(key):
            payload = {'sessionId': SID, 'expectedRunId': str(case.launch.run_id), 'text': key,
                       'expectedConfigurationRevision': 0, 'expectedControlRevision': 2, 'ifBusy': 'queue'}
            registered = (await call(client, 'operation.register', {'expectedRunId': str(case.launch.run_id),
                'registrationKey': key, 'method': 'turn.submit',
                'payloadDigest': p.operation_digest('turn.submit', payload)}))['result']
            submitted = await call(client, 'turn.submit', {**payload, 'operationId': registered['operationId']})
            assert 'result' in submitted, submitted
            return submitted['result']
        active = await asyncio.wait_for(submit('active-private-task'), 3)
        queued = await asyncio.wait_for(submit('queued-private-task'), 3)
        assert active['state'] == 'accepted' and queued['state'] == 'queued'
        assert len(list(case.broker_state.staging.root.glob('*.payload'))) == 1
        original_principal, original_relay = case.ctx.principal, case.ctx.relay_principal
        def service_principal(request):
            request.state.cookie_authenticated = False
            return case.resolve()[0]
        case.ctx.principal = service_principal
        case.ctx.relay_principal = lambda _db, request: service_principal(request)
        denied = await call(client, 'operation.cancel', {'operationId': queued['operationId'],
            'expectedRevision': queued['revision']})
        assert denied['error']['data']['code'] == 'NOT_FOUND_OR_DENIED'
        assert len(case.launch.grants) == 1
        assert len(list(case.broker_state.staging.root.glob('*.payload'))) == 1
        with case.db() as db:
            assert db.get(RelayOperation, queued['operationId']).receipt['state'] == 'queued'
        case.ctx.principal, case.ctx.relay_principal = original_principal, original_relay
        cancelled = await call(client, 'operation.cancel', {'operationId': queued['operationId'],
            'expectedRevision': queued['revision']})
        assert cancelled['result']['reason'] == 'cancelled'
        # Cancellation reclaims unsent bytes without waiting for the active
        # provider turn or dispatching another native request.
        assert not case.launch.grants
        assert not list(case.broker_state.staging.root.glob('*.payload'))
        with case.db() as db:
            assert db.get(RelaySession, SID).active_operation_id == active['operationId']
            assert db.get(RelayOperation, active['operationId']).receipt['state'] == 'accepted'
            from sqlalchemy import select

            from acc.server.models import AuditLog
            audit = db.scalars(select(AuditLog).where(AuditLog.action == 'relay.operation.cancel')).all()
            assert len(audit) == 1 and audit[0].detail['operation_id'] == queued['operationId']
            assert audit[0].actor == 'alex' and audit[0].detail['channel'] == 'chat'
    assert sum(method == 'turn/start' for method, _ in case.owners[0].calls) == 1


async def test_public_cancel_of_persisted_registration_needs_no_runtime(public_case):
    from acc.server.session_relay.hub import Hub

    case = public_case
    operation = Hub(case.ctx).register(case.resolve, SID, str(case.launch.run_id), 'persisted-cancel',
        'turn.submit', p.sha256_digest(b'never-staged'))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(case.app), base_url='http://fixture') as client:
        await attach(client, 'persisted-only-cancel')
        run = case.manager.runs.pop(SID)
        try:
            result = await call(client, 'operation.cancel', {'operationId': operation.operation_id,
                'expectedRevision': operation.receipt.revision})
            assert result['result']['state'] == 'failed' and result['result']['reason'] == 'cancelled'
        finally:
            case.manager.runs[SID] = run
    assert not case.launch.grants
    assert not any(method == 'turn/start' for method, _ in case.owners[0].calls)


@pytest.mark.parametrize('path', ['terminal?expectedRunId=1&attachmentId=fixture'])
async def test_security_s3_feature_off_reads_refuse_before_runtime_io(public_case, monkeypatch, path):
    case = public_case
    monkeypatch.setenv('ACC_FEATURE_SESSION_RELAY', 'false')
    calls=[]
    async def unexpected(*args):
        calls.append(args)
        return {}
    monkeypatch.setattr(case.manager, 'read_terminal', unexpected)
    monkeypatch.setattr(case.manager, 'recover_application', unexpected)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(case.app),base_url='http://fixture') as client:
        response=await client.get(f'/api/sessions/{SID}/relay/{path}')
    assert response.status_code == 409 and not calls
    assert 'turned off' in response.text


@pytest.mark.parametrize('method', ['turn.interrupt', 'decision.reply'])
async def test_security_s3_feature_off_operation_reads_only_stored_receipt(public_case, monkeypatch, method):  # noqa: F811
    from types import SimpleNamespace

    from acc.server.session_relay import store
    c = public_case
    monkeypatch.setenv('ACC_FEATURE_SESSION_RELAY', 'false')
    receipt = {'state': 'uncertain', 'method': method, 'actor': p.to_wire(c.resolve()[1])}
    monkeypatch.setattr(store, 'operation', lambda *_args: SimpleNamespace(receipt=receipt))
    calls = []
    async def forbidden(*args):
        calls.append(args)
        raise AssertionError('feature-off stored receipt triggered broker IO')
    monkeypatch.setattr(c.manager, 'observe_operation', forbidden)
    monkeypatch.setattr(c.manager, 'recover_application', forbidden)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url='http://fixture') as client:
        response = await client.get(f'/api/sessions/{SID}/relay/operations/stored')
    assert response.status_code == 200 and response.json()['state'] == 'uncertain'
    assert response.json()['method'] == method and not calls
