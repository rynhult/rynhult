"""Production immutable proof writer and fresh-manager read-only recovery."""
import threading

import pytest
from test_session_relay_claude_agent import until
from test_session_relay_claude_public import mutate, public_claude  # noqa: F401
from test_session_relay_claude_public_decisions import pending_hook, qualify
from test_session_relay_ownership import SID, _decision, ownership_case  # noqa: F401
from test_session_relay_public_writer import attach, call
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.broker import application_proofs
from acc.server.models import RelayOperation
from acc.server.session_relay.runtime import RuntimeManager
from acc.session_relay import protocol as p


@pytest.mark.parametrize('fault', ['none', 'none:http', 'none:rpc', 'missing', 'actor', 'digest', 'binding', 'after-await', 'symlink'])
async def test_unpublished_application_recovers_without_a_producer(public_claude, monkeypatch, fault):  # noqa: F811
    c = public_claude
    fault, _, route = fault.partition(':')
    qualify(c, monkeypatch)
    monkeypatch.setattr('acc.broker.relay_ownership.APPLICATION_OBSERVATION_SECONDS', 0)
    await attach(c.client, 'unpublished-proof', ['decisions', 'switch'])
    ready = threading.Event()
    hooks = c.owned_factory.hooks
    def prepare(payload, identifier):
        def proof():
            if not ready.is_set():
                raise p.RelayError(p.ErrorCode.BUSY)
            return 'fixture-conversation:user:provider:' + identifier + ':fixture-tool'
        return proof
    hooks.prepare_application = prepare
    pending, decision = await pending_hook(c)
    response, identifier = await mutate(c, 'decision.reply', {'decisionId': decision['decisionId'], 'choiceId': 'decline',
        'expectedRevision': decision['revision'], 'actionDigest': decision['actionDigest']}, 'unpublished-original')
    assert response['result'] == {'outcome': 'unknown'}
    await pending
    broker = c.broker_state
    await until(lambda: not broker.application_tasks)
    original_publish = broker.publish
    async def lose(value):
        if value.get('kind') == 'decision.applied':
            raise ConnectionError('unpublished conclusive application')
        return await original_publish(value)
    broker.publish = lose
    ready.set()
    broker.reconcile_late_applications()
    await until(lambda: not broker.application_tasks)
    file = broker.application_proofs.root / application_proofs.filename(identifier)
    assert file.is_file() and _decision(c, decision['decisionId'])['status'] == 'outcome_unknown'
    import json
    if fault == 'missing':
        file.unlink()
    elif fault == 'symlink':
        original = file.with_suffix('.original')
        file.rename(original)
        file.symlink_to(original)
    elif fault in ('actor', 'digest', 'binding'):
        fact = json.loads(file.read_text())
        if fault == 'actor':
            fact['operation']['actor']['principal'] = 'another-person'
        elif fault == 'digest':
            fact['operation']['payloadDigest'] = p.sha256_digest(b'wrong')
        else:
            fact['binding']['account'] = 'another-account'
        file.write_text(json.dumps(fact))
    count = len(c.owned_factory.owners)
    await c.manager.close()
    await until(lambda: not c.factory.active)
    if fault == 'after-await':
        original_read = application_proofs.read
        def revoke(*args):
            result = original_read(*args)
            c.state['revoked'] = True
            return result
        monkeypatch.setattr(application_proofs, 'read', revoke)
    fresh = RuntimeManager(c.ctx)
    try:
        if route:
            c.ctx.relay_runtime = fresh
            if route == 'http':
                response = await c.client.get(f'/api/sessions/{SID}/relay/operations/{identifier}')
                assert response.status_code == 200, response.text
                receipt = response.json()
            else:
                response = await call(c.client, 'operation.get', {'operationId': identifier})
                assert 'result' in response, response
                receipt = response['result']
            assert receipt['state'] == 'accepted' and receipt['operationId'] == identifier
            result = True
        else:
            result = await fresh.recover_application(c.resolve, SID, identifier)
        assert result is (fault == 'none')
        assert len(c.owned_factory.owners) == count and not c.factory.active and not fresh.runs
        assert _decision(c, decision['decisionId'])['status'] == ('denied' if fault == 'none' else 'outcome_unknown')
        with c.db() as db:
            assert db.get(RelayOperation, identifier).receipt['state'] == ('accepted' if fault == 'none' else 'uncertain')
        assert not fresh.application_recovery.tasks
    finally:
        await fresh.close()
