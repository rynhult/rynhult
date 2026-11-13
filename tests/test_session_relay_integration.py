"""M2 hub HTTP/lifecycle integration with an isolated database and no provider."""
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from relay_helpers import setup
from sqlalchemy import select

from acc.server.api.session_relay import router
from acc.server.db import write_intent
from acc.server.models import RelayEvent, RelayOperation, RelaySession, Session, utcnow
from acc.server.session_relay import history, store
from acc.server.session_relay.maintenance import maintain
from acc.session_relay import protocol as p


@pytest.fixture
def case(tmp_path):
    case = setup(tmp_path)
    case.auth_count = 0
    def principal(request: Request):
        case.auth_count += 1
        request.state.cookie_authenticated = True
        request.state.session_hash = case.state.get('login', 'fixture-login')
        return case.resolve()[0]
    case.ctx.principal = principal
    case.ctx.relay_principal = lambda _db, request: principal(request)
    case.ctx.supervisor = SimpleNamespace(vault=SimpleNamespace(redactor=lambda db, keys: SimpleNamespace(text=lambda s: s)))
    case.app = FastAPI()
    case.app.include_router(router(case.ctx))
    yield case
    case.engine.dispose()


async def call(client, method, params):
    return await client.post('/api/sessions/relay/relay', json={
        'jsonrpc': '2.0', 'id': 'request', 'method': 'ry/' + method, 'params': params})


async def attach(client):
    response = await call(client, 'initialize', {'major': 1, 'minor': 0, 'features': ['queue', 'terminal'], 'clientId': 'fixture'})
    assert response.status_code == 200, response.text
    initialized = response.json()['result']
    assert initialized['features'] == []
    client.headers['X-ACC-Relay-Connection'] = initialized['connectionToken']
    result = (await call(client, 'session.attach', {'sessionId': 'relay', 'clientId': 'fixture', 'connectionSeq': 1})).json()
    return result['result']


async def test_real_hub_http_two_observers_scope_no_effect_and_unsupported_registration(case):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(case.app), base_url='http://fixture') as client:
        attached = await attach(client)
        assert attached['snapshot']['capabilities'] == {}
        assert attached['snapshot']['history'] == 'unsupported'
        before = case.auth_count
        snapshot = await client.get('/api/sessions/relay/relay')
        assert snapshot.status_code == 200 and case.auth_count == before + 1
        assert snapshot.headers['cache-control'] == 'no-store'
        params = {'sessionId': 'relay', 'attachmentId': attached['attachmentId']}
        assert 'result' in (await call(client, 'events.read', params)).json()
        denied = await call(client, 'operation.register', {'sessionId': 'relay', 'expectedRunId': '1',
            'registrationKey': 'send', 'method': 'turn.submit', 'payloadDigest': p.sha256_digest(b'text')})
        assert denied.json()['error']['data']['code'] == 'UNSUPPORTED'
        # A new browser attachment is an observer; it does not become controller.
        async with httpx.AsyncClient(transport=httpx.ASGITransport(case.app), base_url='http://fixture') as second:
            other = await attach(second)
            assert other['attachmentId'] != attached['attachmentId']
            assert 'error' in (await call(second, 'events.read', params)).json()
        case.state['login'] = 'replacement-login'
        assert 'error' in (await call(client, 'session.snapshot', {'sessionId': 'relay'})).json()
    with case.db() as db:
        assert not list(db.scalars(select(RelayOperation)))
        assert db.get(RelaySession, 'relay').controller is None


async def test_preference_cas_person_only_no_runtime_effect(case):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(case.app), base_url='http://fixture') as client:
        assert (await client.get('/api/session-relay/preference')).json() == {'view': 'terminal', 'revision': 0}
        result = await client.put('/api/session-relay/preference', json={'view': 'chat', 'expectedRevision': 0})
        assert result.json() == {'view': 'chat', 'revision': 1}
        assert (await client.put('/api/session-relay/preference', json={'view': 'terminal', 'expectedRevision': 0})).status_code == 409
        assert (await client.put('/api/session-relay/preference', json={'view': 'chat', 'expectedRevision': True})).status_code == 422
    with case.db() as db:
        assert db.get(RelaySession, 'relay').event_seq == 0


def test_periodic_retention_runs_with_flag_off_and_preserves_receipts(case):
    @write_intent
    def seed():
        with case.db() as db:
            row = store.lock(db, 'relay')
            event = store.append(db, row, 'message.upsert', {'text': 'old'})
            event.created_at = utcnow() - timedelta(days=31)
            db.commit()
    seed()
    maintain(case.ctx, limit=1)
    with case.db() as db:
        row = db.get(RelaySession, 'relay')
        assert row.floor_seq == 1
        assert [e.event['kind'] for e in db.scalars(select(RelayEvent))] == ['history.gap']
        assert row.used_bytes == sum(e.byte_size for e in db.scalars(select(RelayEvent)))
    maintain(case.ctx, limit=1)
    assert case.ctx._relay_retention_after == ''


def test_public_actor_projection_does_not_change_authority_binding():
    actor = {'kind': 'person', 'principal': 'alex', 'channel': 'phone', 'device': 'private-device'}
    value = {'controller': actor, 'decision': {'winner': actor}, 'items': [actor]}
    public = history.public_actors(value)
    assert 'private-device' not in str(public)
    assert public['decision']['winner']['principal'] == 'alex'
    assert value['controller']['device'] == 'private-device'


async def test_enrolled_reconcile_cannot_use_legacy_resume_or_probe_provider(case):
    from acc.server.reconcile import ReconcileMixin
    supervisor = SimpleNamespace(settings=case.ctx.settings, _recovery_needed=lambda db, session, reason: setattr(session, 'last_error', reason))
    with case.db() as db:
        session = db.get(Session, 'relay')
        await ReconcileMixin._attempt_resume(supervisor, db, session, None)
        assert 'explicit qualified recovery' in session.last_error


async def test_sleep_does_not_seal_or_stop_relay(case):
    from acc.server.supervisor import Supervisor
    with case.db() as db:
        assert not await Supervisor._sleep_ready(SimpleNamespace(), db, db.get(Session, 'relay'))
        assert not db.get(RelaySession, 'relay').sealed


def test_restart_recovery_fences_once_and_never_requeues_claim(case):
    from acc.server.session_relay.hub import Hub
    from acc.server.session_relay.maintenance import recover
    hub = Hub(case.ctx)
    digest = p.sha256_digest(b'fixture')
    receipt = hub.register(case.resolve, 'relay', '1', 'restart', 'turn.submit', digest)
    hub.queue(case.resolve, 'relay', receipt.operation_id, expected_run='1', digest=digest)
    hub.claim(case.resolve, 'relay', receipt.operation_id)
    recover(case.ctx)
    with case.db() as db:
        epoch = db.get(RelaySession, 'relay').authority_epoch
        assert db.get(RelayOperation, receipt.operation_id).receipt['state'] == 'uncertain'
    recover(case.ctx)
    with case.db() as db:
        assert db.get(RelaySession, 'relay').authority_epoch == epoch
        assert db.get(RelaySession, 'relay').active_operation_id == receipt.operation_id


def test_retention_can_use_finalization_headroom_when_normal_admission_is_full(case, monkeypatch):
    @write_intent
    def seed():
        with case.db() as db:
            row = store.lock(db, 'relay')
            old = store.append(db, row, 'message.upsert', {'text': 'old'})
            old.created_at = utcnow() - timedelta(days=31)
            store.append(db, row, 'session.stateChanged', {'state': 'retained outcome'}, priority=True)
            db.commit()
    seed()
    monkeypatch.setattr(store, 'MAX_RETAINED_BYTES', 4096)
    monkeypatch.setattr(store, 'FINALIZATION_RESERVE', 4080)
    # Remaining finalization metadata exceeds the normal cap. Publishing the
    # retention gap must not roll back deletion of the expired messages.
    maintain(case.ctx)
    with case.db() as db:
        assert db.get(RelaySession, 'relay').floor_seq == 1
        assert [e.event['kind'] for e in db.scalars(select(RelayEvent).order_by(RelayEvent.seq))] == [
            'session.stateChanged', 'history.gap']
