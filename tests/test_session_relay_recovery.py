"""Cross-store crash boundaries using the real main hub and broker admission journal."""
from dataclasses import replace

import pytest
from relay_helpers import setup

from acc.broker.session_relay import AdmissionBroker, AdmissionJournal
from acc.server.db import write_intent
from acc.server.models import RelayOperation, RelaySession
from acc.server.session_relay import store
from acc.server.session_relay.hub import Hub
from acc.server.session_relay.maintenance import recover
from acc.session_relay import protocol as p
from acc.session_relay.adapters.base import AdapterOperation, AdmissionResult, Outcome


@pytest.mark.parametrize('provider_accepted', [False, True])
async def test_main_restart_and_broker_reconnect_never_send_claim_again(tmp_path, provider_accepted):
    case = setup(tmp_path)
    journal = AdmissionJournal(tmp_path / 'private', clock=lambda: 1000)
    broker = AdmissionBroker(journal, verify=lambda op: allowed())
    async def allowed():
        return True
    try:
        hub = Hub(case.ctx)
        params = {'sessionId': 'relay', 'expectedRunId': '1', 'text': 'Artificial task',
                  'expectedConfigurationRevision': 0, 'ifBusy': 'reject'}
        digest = p.operation_digest('turn.submit', params)
        registered = hub.register(case.resolve, 'relay', '1', 'crash', 'turn.submit', digest)
        hub.queue(case.resolve, 'relay', registered.operation_id, expected_run='1', digest=digest)
        hub.claim(case.resolve, 'relay', registered.operation_id)
        fence = p.Fence(1, broker.incarnation, 'fixture-main')
        await broker.enroll(fence, 'link')
        operation = AdapterOperation(registered.operation_id, 'relay', '1', 'turn.submit', digest,
                                     case.resolve()[1], fence, 2000, params)
        sent = []
        async def send(op):
            sent.append(op.operation_id)
            if provider_accepted:
                evidence = p.Evidence(p.EvidenceKind.PROVIDER_ADMISSION, op.operation_id, op.run_id, 'fixture-native')
                return AdmissionResult(Outcome.ACCEPTED, evidence, 'fixture-turn')
            return AdmissionResult(Outcome.UNKNOWN)
        # Simulate response loss after the broker's durable claim/admission but
        # before the main receipt transaction. Main cannot infer acceptance.
        result = await broker.dispatch(operation, send)
        recover(case.ctx)
        with case.db() as db:
            assert db.get(RelayOperation, registered.operation_id).receipt['state'] == 'uncertain'
        again = await broker.dispatch(operation, send)
        await broker.disconnect('link')
        await broker.enroll(replace(fence, authority_epoch=2), 'reconnected')
        with pytest.raises(p.RelayError):
            await broker.dispatch(operation, send)
        with pytest.raises(p.RelayError):
            await broker.dispatch(replace(operation, fence=replace(fence, authority_epoch=2)), send)
        assert sent == [registered.operation_id]
        assert again.outcome is result.outcome
        # Qualified late evidence may settle the original receipt, never replay.
        if provider_accepted:
            @write_intent
            def settle():
                with case.db() as db:
                    row = store.lock(db, 'relay')
                    store.settle(db, row, db.get(RelayOperation, registered.operation_id),
                                 evidence=again.evidence, accepted=True)
                    db.commit()
            settle()
        stale = replace(operation, fence=replace(fence, authority_epoch=0))
        with pytest.raises(p.RelayError):
            await broker.dispatch(stale, send)
        assert len(sent) == 1
        with case.db() as db:
            assert db.get(RelaySession, 'relay').active_operation_id == registered.operation_id
    finally:
        journal.close()
        case.engine.dispose()
