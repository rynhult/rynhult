from datetime import timedelta

import pytest
from relay_helpers import ACTOR, DIGEST, setup
from sqlalchemy import select

from acc.server.db import write_intent
from acc.server.models import RelayEvent, RelayOperation, RelaySession, utcnow
from acc.server.session_relay import history, store
from acc.session_relay import protocol as p


@pytest.fixture
def case(tmp_path):
    case = setup(tmp_path)
    yield case
    case.engine.dispose()


@write_intent
def register(case, key='key'):
    with case.db() as db:
        row = store.lock(db, 'relay')
        result = store.register(db, row, ACTOR, key, 'turn.submit', DIGEST, {})
        db.commit()
        return result


def test_registration_transaction_dedup_and_payload_integrity(case):
    first = register(case)
    assert register(case).operation_id == first.operation_id
    with case.db() as db:
        assert len(db.scalars(select(RelayOperation)).all()) == 1
        assert len(db.scalars(select(RelayEvent)).all()) == 1
    with pytest.raises(p.RelayError) as error, case.db() as db:
        store.register(db, store.lock(db, 'relay'), ACTOR, 'key', 'turn.submit', p.sha256_digest(b'changed'), {})
    assert error.value.code is p.ErrorCode.INTEGRITY_FAILURE
    with case.db() as db:
        row = store.lock(db, 'relay')
        store.transition(db, row, db.get(RelayOperation, first.operation_id), p.ReceiptState.SUBMITTING)
        db.rollback()
    with case.db() as db:
        assert db.get(RelayOperation, first.operation_id).receipt['state'] == 'queued'
        assert db.get(RelaySession, 'relay').event_seq == 1
        assert len(db.scalars(select(RelayEvent)).all()) == 1


def test_scoped_cursors_receipts_redaction_and_source_reset(case):
    first = register(case)
    pages = history.History(b'x' * 32)
    with case.db() as db:
        row = store.lock(db, 'relay')
        history.message(db, row, item_id='item', role='assistant', complete_text='split-secret',
                        source_epoch=1, source_revision=1, mask=lambda s: s.replace('split-secret', '[redacted]'))
        history.message(db, row, item_id='item', role='assistant', complete_text='split-secret',
                        source_epoch=1, source_revision=1, mask=lambda s: s.replace('split-secret', '[redacted]'))
        db.commit()
    with case.db() as db:
        row = store.lock(db, 'relay')
        page = pages.page(db, row, 'alex-device', actor=ACTOR, mask=lambda s: s)
        assert len(page['events']) == 2
        assert page['events'][1]['payload']['text'] == '[redacted]'
        foreign = p.Actor(p.ActorKind.PERSON, 'sam', p.Channel.CHAT)
        assert len(pages.page(db, row, 'sam-device', actor=foreign, mask=lambda s: s)['events']) == 1
        for scope in ('sam-device', 'alex-other-device'):
            with pytest.raises(p.RelayError):
                pages.page(db, row, scope, actor=ACTOR, cursor=page['cursor'], mask=lambda s: s)
        with pytest.raises(p.RelayError):
            store.operation(db, 'relay', first.operation_id, foreign)
        history.reset(db, row)
        with pytest.raises(p.RelayError) as error:
            pages.page(db, row, 'alex-device', actor=ACTOR, cursor=page['cursor'], mask=lambda s: s)
        assert error.value.code is p.ErrorCode.CURSOR_RESET
        db.commit()


def test_expired_keys_quota_tombstones_and_deleted_enrollment(case, monkeypatch):
    result = register(case)
    with case.db() as db:
        op = db.get(RelayOperation, result.operation_id)
        op.deadline = utcnow() - timedelta(days=1)
        db.commit()
    assert register(case).operation_id == result.operation_id  # expired execution is no new registration
    monkeypatch.setattr(store, 'MAX_REGISTRATIONS', 1)
    with pytest.raises(p.RelayError) as error:
        register(case, 'other')
    assert error.value.code is p.ErrorCode.LIMIT
    with case.db() as db:
        assert db.get(RelaySession, 'relay').event_seq == 1
        db.delete(db.get(RelaySession, 'relay'))
        db.commit()
    with case.db() as db:
        assert db.get(RelayOperation, result.operation_id) is None
        with pytest.raises(p.RelayError):
            store.lock(db, 'relay')


def test_history_bounds_pruning_and_storage_refusal_are_atomic(case, monkeypatch):
    register(case)
    pages = history.History(b'x' * 32)
    with case.db() as db:
        row = store.lock(db, 'relay')
        cursor = pages.cursor(row, 'viewer', 0)
        store.prune(db, row, before=utcnow() + timedelta(seconds=1))
        db.commit()
    with case.db() as db:
        row = store.lock(db, 'relay')
        with pytest.raises(p.RelayError):
            pages.page(db, row, 'viewer', actor=ACTOR, cursor=cursor, mask=lambda s: s)
        assert pages.page(db, row, 'viewer', actor=ACTOR, mask=lambda s: s)['gap']
    monkeypatch.setattr(store, 'MAX_EVENTS', 1)
    with pytest.raises(p.RelayError):
        register(case, 'will-rollback')
    with case.db() as db:
        assert len(db.scalars(select(RelayOperation)).all()) == 1


def test_invalid_expired_decisions_and_pending_quota_keep_winner_empty(case, monkeypatch):
    from dataclasses import replace
    decision = p.Decision('question', 'relay', '1', p.DecisionKind.QUESTION, DIGEST, 'Review?',
                          (p.DecisionChoice('yes', 'Yes', p.ChoiceEffect.ANSWER),), 9999999999999, 0)
    with case.db() as db:
        row = store.lock(db, 'relay')
        store.add_decision(db, row, decision)
        db.commit()
    with case.db() as db:
        row = store.lock(db, 'relay')
        for choice in ('invalid',):
            with pytest.raises(p.RelayError) as error:
                store.reserve_decision(db, row, 'question', ACTOR, choice_id=choice, expected_revision=0,
                                       action_digest=DIGEST)
            assert error.value.code is p.ErrorCode.POLICY_REFUSED
        monkeypatch.setattr(p, 'MAX_PENDING_DECISIONS', 1)
        with pytest.raises(p.RelayError) as error:
            store.add_decision(db, row, replace(decision, decision_id='other'))
        assert error.value.code is p.ErrorCode.LIMIT
        from acc.server.models import RelayDecision
        record = db.get(RelayDecision, ('relay', 'question'))
        record.decision = p.to_wire(replace(decision, expires_at_ms=1))
        db.commit()
    with case.db() as db:
        with pytest.raises(p.RelayError) as error:
            store.reserve_decision(db, store.lock(db, 'relay'), 'question', ACTOR, choice_id='yes',
                                   expected_revision=0, action_digest=DIGEST)
        assert error.value.code is p.ErrorCode.DECISION_EXPIRED
        assert db.get(RelayDecision, ('relay', 'question')).decision.get('winner') is None
