"""Conclusive rejection releases only its own current turn, never replacement work."""
import pytest
from relay_helpers import ACTOR, DIGEST
from test_session_relay_store import case  # noqa: F401

from acc.server.db import write_intent
from acc.server.models import SessionRun, utcnow
from acc.server.session_relay import store
from acc.session_relay import protocol as p


@pytest.mark.parametrize('state', ['submitting', 'uncertain'])
@pytest.mark.parametrize('case_kind', ['rejected', 'accepted', 'replacement-run', 'replacement-turn', 'interrupt'])
@write_intent
def test_settlement_releases_only_conclusively_rejected_current_turn(case, state, case_kind):  # noqa: F811
    with case.db() as db:
        row = store.lock(db, 'relay')
        method = 'turn.interrupt' if case_kind == 'interrupt' else 'turn.submit'
        registered = store.register(db, row, ACTOR, 'claim', method, DIGEST, {})
        op = store.operation(db, 'relay', registered.operation_id, ACTOR)
        store.transition(db, row, op, p.ReceiptState.SUBMITTING)
        if state == 'uncertain':
            store.transition(db, row, op, p.ReceiptState.UNCERTAIN)
        row.active_operation_id = op.id
        if case_kind == 'replacement-run':
            replacement = SessionRun(session_id='relay', reason='replacement', verified_at=utcnow())
            db.add(replacement)
            db.flush()
            row.current_run_id = replacement.id
        elif case_kind in ('replacement-turn', 'interrupt'):
            row.active_operation_id = 'replacement-operation'
        previous = row.active_operation_id
        accepted = case_kind == 'accepted'
        proof = p.Evidence(p.EvidenceKind.PROVIDER_ADMISSION if accepted else p.EvidenceKind.BROKER_REFUSAL,
                           op.id, str(op.run_id))
        receipt = store.settle(db, row, op, evidence=proof, accepted=accepted)
        assert receipt.state is (p.ReceiptState.ACCEPTED if accepted else p.ReceiptState.FAILED)
        assert row.active_operation_id == (None if case_kind == 'rejected' else previous)
        db.rollback()


@write_intent
def test_foreign_rejection_evidence_cannot_release_current_claim(case):  # noqa: F811
    with case.db() as db:
        row = store.lock(db, 'relay')
        registered = store.register(db, row, ACTOR, 'claim', 'turn.submit', DIGEST, {})
        op = store.operation(db, 'relay', registered.operation_id, ACTOR)
        store.transition(db, row, op, p.ReceiptState.SUBMITTING)
        row.active_operation_id = op.id
        with pytest.raises(ValueError, match='another operation or run'):
            store.settle(db, row, op, evidence=p.Evidence(p.EvidenceKind.BROKER_REFUSAL, 'foreign-operation', str(op.run_id)), accepted=False)
        assert row.active_operation_id == op.id and op.receipt['state'] == 'submitting'
        db.rollback()
