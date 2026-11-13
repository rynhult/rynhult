import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from fastapi import HTTPException
from relay_helpers import DIGEST, setup
from sqlalchemy import select

from acc.server.api.context import Principal
from acc.server.db import write_intent
from acc.server.models import AuditLog, RelayDecision, RelayOperation, RelaySession, User
from acc.server.session_relay import store
from acc.server.session_relay.hub import Hub, refuse_legacy, seal_for_stop
from acc.session_relay import protocol as p


@pytest.fixture
def case(tmp_path):
    case = setup(tmp_path)
    case.hub = Hub(case.ctx)
    yield case
    case.engine.dispose()


def register(case, key='key', method='turn.submit'):
    return case.hub.register(case.resolve, 'relay', '1', key, method, DIGEST)


def queued(case, key='key'):
    op = register(case, key)
    case.hub.queue(case.resolve, 'relay', op.operation_id, expected_run='1', digest=DIGEST, if_busy='queue')
    return op.operation_id


def test_fifo_single_claim_and_no_replay_after_lost_response(case):
    first = queued(case)
    assert case.hub.claim(case.resolve, 'relay', first).state is p.ReceiptState.SUBMITTING
    assert case.hub.claim(case.resolve, 'relay', first).state is p.ReceiptState.SUBMITTING
    second = queued(case, 'second')
    with pytest.raises(p.RelayError) as error:
        case.hub.claim(case.resolve, 'relay', second)
    assert error.value.code is p.ErrorCode.BUSY
    with case.db() as db:
        row = store.lock(db, 'relay')
        store.recover(db, row)
        db.commit()
    assert case.hub.claim(case.resolve, 'relay', first).state is p.ReceiptState.UNCERTAIN
    assert case.hub.claim(case.resolve, 'relay', second).reason is p.FailureReason.AUTHORITY_ENDED
    with case.db() as db:
        assert db.get(RelayOperation, first).receipt['state'] == 'uncertain'
        assert len(db.scalars(select(AuditLog).where(AuditLog.action == 'relay.refused')).all()) == 1


async def test_post_await_revocation_and_epoch_change_never_claim(case):
    identifier = queued(case)
    async def revoke():
        await asyncio.sleep(0)
        case.state['revoked'] = True
    with pytest.raises(HTTPException):
        await case.hub.prepare_claim(case.resolve, 'relay', identifier, revoke)
    case.state['revoked'] = False
    async def change():
        with case.db() as db:
            db.get(RelaySession, 'relay').authority_epoch += 1
            db.commit()
    with pytest.raises(p.RelayError):
        await case.hub.prepare_claim(case.resolve, 'relay', identifier, change)
    with case.db() as db:
        assert db.get(RelayOperation, identifier).receipt['state'] == 'queued'


def test_disconnect_needs_explicit_confirmation_and_cancel_is_preclaim_only(case):
    identifier = queued(case)
    case.hub.disconnect(case.resolve, 'relay')
    with pytest.raises(p.RelayError):
        case.hub.claim(case.resolve, 'relay', identifier)
    case.hub.reconfirm(case.resolve, 'relay', identifier)
    case.hub.claim(case.resolve, 'relay', identifier)
    with pytest.raises(p.RelayError):
        case.hub.cancel(case.resolve, 'relay', identifier, 1)


def test_decision_race_first_valid_answer_and_atomic_receipt(case):
    @write_intent
    def seed():
        with case.db() as db:
            db.get(User, 2).role = 'owner'
            store.add_decision(db, store.lock(db, 'relay'), p.Decision('question', 'relay', '1', p.DecisionKind.QUESTION,
                               DIGEST, 'Keep notes?', (p.DecisionChoice('yes', 'Yes', p.ChoiceEffect.ANSWER),),
                               9999999999999, 0))
            db.commit()
    seed()
    other_actor = p.Actor(p.ActorKind.PERSON, 'sam', p.Channel.PHONE, 'fixture-device')
    other = lambda: (Principal('sam', 'owner', 'fixture', 2), other_actor)
    answer_digest = p.operation_digest('decision.reply', {
        'sessionId': 'relay', 'expectedRunId': '1', 'decisionId': 'question',
        'choiceId': 'yes', 'expectedRevision': 0, 'actionDigest': DIGEST})
    one = case.hub.register(case.resolve, 'relay', '1', 'one', 'decision.reply', answer_digest)
    two = case.hub.register(other, 'relay', '1', 'two', 'decision.reply', answer_digest)
    def answer(resolve, op, choice='yes'):
        return case.hub.answer(resolve, 'relay', 'question', expected_run='1', attached=True, choice_id=choice,
                               revision=0, digest=DIGEST, operation_id=op)
    with pytest.raises(p.RelayError):
        answer(case.resolve, one.operation_id, 'invalid')
    with ThreadPoolExecutor(2) as executor:
        futures = [executor.submit(answer, resolver, op) for resolver, op in
                   [(case.resolve, one.operation_id), (other, two.operation_id)]]
        results = []
        for future in futures:
            try:
                results.append(future.result())
            except p.RelayError as error:
                assert error.code is p.ErrorCode.DECISION_RESOLVED
    assert len(results) == 1
    with case.db() as db:
        assert db.get(RelayDecision, ('relay', 'question')).decision['winner']['principal'] == results[0].winner.principal
        ops = db.scalars(select(RelayOperation)).all()
        assert sorted(op.receipt['state'] for op in ops) == ['queued', 'submitting']
        assert not any('Keep notes?' in str(a.detail) for a in db.scalars(select(AuditLog)))
        store.recover(db, store.lock(db, 'relay'))
        db.commit()
        assert db.get(RelayDecision, ('relay', 'question')).decision['status'] == 'outcome_unknown'
    with pytest.raises(p.RelayError) as error:
        answer(case.resolve, one.operation_id)
    assert error.value.code is p.ErrorCode.STALE_AUTHORITY


def test_draft_takeover_cas_and_unsettled_boundary(case):
    # A terminal draft belongs to the terminal; metadata takeover never reads,
    # clears or submits its bytes. No TerminalHost or broker is called in M1.
    with pytest.raises(p.RelayError):
        case.hub.take(case.resolve, 'relay', revision=0, reason='Inspect the draft', drain_settled=False)
    assert case.hub.take(case.resolve, 'relay', revision=0, reason='Inspect the draft', drain_settled=True) == 1
    with pytest.raises(p.RelayError):
        case.hub.take(case.resolve, 'relay', revision=0, reason='Competing take', drain_settled=True)


def test_stop_fences_receipts_and_legacy_resume(case):
    identifier = queued(case)
    seal_for_stop(case.ctx, 'relay')
    with case.db() as db:
        assert db.get(RelayOperation, identifier).receipt['reason'] == 'run_ended'
        assert db.get(RelaySession, 'relay').sealed
        from acc.server.supervisor import LaunchRefused
        with pytest.raises(LaunchRefused):
            refuse_legacy(db, 'relay')
    with pytest.raises(p.RelayError):
        case.hub.claim(case.resolve, 'relay', identifier)


def test_off_refuses_registration_and_configuration_without_provider_calls(case):
    with pytest.raises(p.RelayError):
        register(case, method='configuration.request')
    case.ctx.settings = replace(case.ctx.settings, feature_overrides={'session_relay': False})
    with pytest.raises(p.RelayError):
        register(case)


def test_queue_deadline_expiry_and_one_unsent_per_actor(case):
    from datetime import timedelta
    identifier = queued(case)
    other = register(case, 'other')
    with pytest.raises(p.RelayError) as error:
        case.hub.queue(case.resolve, 'relay', other.operation_id, expected_run='1', digest=DIGEST, if_busy='queue')
    assert error.value.code is p.ErrorCode.LIMIT
    with case.db() as db:
        db.get(RelayOperation, identifier).deadline = store.utcnow() - timedelta(seconds=1)
        db.commit()
    assert case.hub.claim(case.resolve, 'relay', identifier).reason is p.FailureReason.EXPIRED
    assert register(case).operation_id == identifier


def test_storage_admission_limit_does_not_block_stop_finalization(case, monkeypatch):
    identifier = queued(case)
    monkeypatch.setattr(store, 'MAX_EVENTS', 1)
    with pytest.raises(p.RelayError):
        register(case, 'full')
    seal_for_stop(case.ctx, 'relay')
    with case.db() as db:
        assert db.get(RelaySession, 'relay').sealed
        assert db.get(RelayOperation, identifier).receipt['reason'] == 'run_ended'


async def test_read_api_preserves_history_when_off_and_refuses_invalid_rpc(case):
    from types import SimpleNamespace

    import httpx
    from fastapi import FastAPI, Request

    from acc.server.api.session_relay import router

    identifier = queued(case)
    case.ctx.settings = replace(case.ctx.settings, feature_overrides={'session_relay': False})
    def principal(request: Request):
        request.state.cookie_authenticated = True
        return case.resolve()[0]
    case.ctx.principal = principal
    case.ctx.relay_principal = lambda _db, request: principal(request)
    case.ctx.supervisor = SimpleNamespace(vault=SimpleNamespace(redactor=lambda db, keys: SimpleNamespace(text=lambda s: s)))
    app = FastAPI()
    app.include_router(router(case.ctx))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://fixture') as client:
        snapshot = await client.get('/api/sessions/relay/relay')
        assert snapshot.status_code == 200
        assert snapshot.headers['cache-control'] == 'no-store'
        assert snapshot.json()['capabilities'] == {}
        events = await client.get('/api/sessions/relay/relay/events')
        assert events.status_code == 200
        receipt = await client.get('/api/sessions/relay/relay/operations/' + identifier)
        assert receipt.json()['state'] == 'queued'
        assert (await client.post('/api/sessions/relay/relay', json={'method': 'ry/turn.submit'})).json()['error']['code'] == -32600
        case.state.update(who=Principal('sam', 'member', 'fixture', 2))
        assert (await client.get('/api/sessions/relay/relay')).status_code == 404
        case.state['revoked'] = True
        assert (await client.get('/api/sessions/relay/relay/events')).status_code == 401
