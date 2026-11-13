"""Primary proof recovery cannot allocate a producer for ordinary retained reads."""
from dataclasses import replace

import httpx
import pytest
from test_session_relay_public_writer import SID, attach, call, public_case  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.server.db import write_intent
from acc.server.session_relay import runtime, store
from acc.session_relay import protocol as p


@pytest.mark.parametrize(('method', 'state'), [
    ('turn.submit', 'failed'), ('turn.submit', 'accepted'),
    ('turn.interrupt', 'uncertain'), ('decision.reply', 'uncertain'), ('decision.reply', 'accepted'),
])
async def test_cold_codex_receipts_keep_identity_without_runtime_allocation(public_case, monkeypatch, method, state):  # noqa: F811
    c = public_case

    @write_intent
    def register():
        with c.db() as db:
            row = store.lock(db, SID)
            identifiers = []
            for actor, key in [(c.resolve()[1], 'mine'), (replace(c.resolve()[1], principal='another-person'), 'foreign')]:
                registered = store.register(db, row, actor, key, method, p.sha256_digest(b'cold fixture'), {},
                    methods=frozenset({method}))
                operation = store.operation(db, SID, registered.operation_id, actor)
                if state == 'failed':
                    store.transition(db, row, operation, p.ReceiptState.FAILED, reason=p.FailureReason.CANCELLED)
                else:
                    store.transition(db, row, operation, p.ReceiptState.SUBMITTING)
                    evidence = p.Evidence(p.EvidenceKind.PROVIDER_ADMISSION, operation.id, operation.receipt['runId'], 'fixture-native')
                    store.transition(db, row, operation, p.ReceiptState(state), evidence=evidence if state == 'accepted' else None)
                identifiers.append(operation.id)
            db.commit()
            return identifiers

    mine, foreign = register()
    allocations = []

    def forbidden(_ctx):
        allocations.append('manager')
        raise AssertionError('Retained Codex receipt cannot construct a runtime')

    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url='http://fixture') as client:
        await attach(client, 'cold-receipt-reader')
        await c.manager.close()
        c.ctx.relay_runtime = None
        monkeypatch.setattr(runtime, 'manager', forbidden)
        response = await client.get(f'/api/sessions/{SID}/relay/operations/{mine}')
        assert response.status_code == 200, response.text
        receipt = response.json()
        assert receipt['operationId'] == mine and receipt['state'] == state
        observed = await call(client, 'operation.get', {'operationId': mine})
        assert observed['result'] == receipt
        refused = await client.get(f'/api/sessions/{SID}/relay/operations/{foreign}')
        assert refused.status_code == 404 and mine not in refused.text
        assert (await call(client, 'operation.get', {'operationId': foreign}))['error']['data']['code'] == 'NOT_FOUND_OR_DENIED'
    assert allocations == [] and c.ctx.relay_runtime is None and not c.factory.active
