"""Expired observers recover only original same-owner proof, never native replies."""
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_session_relay_claude_agent import until
from test_session_relay_claude_public import mutate, ownership_read, public_claude  # noqa: F401
from test_session_relay_claude_public_decisions import pending_hook, qualify
from test_session_relay_ownership import SID, _decision, ownership_case  # noqa: F401
from test_session_relay_public_writer import attach
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.server.models import RelayOperation


@pytest.mark.parametrize('fault', ['none', 'publication', 'publication-ack', 'owner', 'run', 'closed', 'owner-after', 'run-after', 'guard-after'])
async def test_original_application_after_observer_expires(public_claude, monkeypatch, fault):  # noqa: F811
    c = public_claude
    qualify(c, monkeypatch)
    monkeypatch.setattr('acc.broker.relay_ownership.APPLICATION_OBSERVATION_SECONDS', 0)
    view = await attach(c.client, 'expired-application-observer', ['decisions', 'switch'])
    broker = c.broker_state
    ready = threading.Event()
    def prepare(payload, identifier):
        def proof():
            from acc.session_relay import protocol as p
            if not ready.is_set():
                raise p.RelayError(p.ErrorCode.BUSY)
            return 'fixture-conversation:user:provider:' + identifier + ':fixture-tool'
        return proof
    hooks = c.owned_factory.hooks
    hooks.prepare_application = prepare
    original_answer = hooks.answer
    answers = []
    async def counted(*args):
        answers.append(True)
        return await original_answer(*args)
    hooks.answer = counted
    pending, decision = await pending_hook(c)
    response, identifier = await mutate(c, 'decision.reply', {'decisionId': decision['decisionId'], 'choiceId': 'decline',
        'expectedRevision': decision['revision'], 'actionDigest': decision['actionDigest']}, 'expired-original-answer')
    assert response['result'] == {'outcome': 'unknown'}
    await pending
    await until(lambda: not broker.application_tasks)
    assert len(broker.application_records) == 1
    ready.set()
    owner, spec, peer = broker.owners.current, broker.spec, broker.peer
    original_publish = broker.publish
    original_guard = broker.guard_check
    original_inspect = hooks.inspect_application
    async def inspect(*args, **kwargs):
        result = await original_inspect(*args, **kwargs)
        if result.get('outcome') == 'accepted':
            if fault == 'owner-after':
                broker.owners.current = SimpleNamespace()
            elif fault == 'run-after':
                broker.spec = replace(spec, run_id='replacement-run')
            elif fault == 'guard-after':
                def denied():
                    from acc.session_relay import protocol as p
                    raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
                broker.guard_check = denied
        return result
    hooks.inspect_application = inspect
    attempts = []
    async def publish(value):
        if value.get('kind') == 'decision.applied':
            attempts.append(True)
            if fault == 'publication' and len(attempts) == 1:
                raise ConnectionError('synthetic publication loss')
        result = await original_publish(value)
        if value.get('kind') == 'decision.applied' and fault == 'publication-ack' and len(attempts) == 1:
            raise ConnectionError('synthetic acknowledgement loss after durable settlement')
        return result
    broker.publish = publish
    try:
        if fault == 'owner':
            broker.owners.current = SimpleNamespace()
        elif fault == 'run':
            broker.spec = replace(spec, run_id='replacement-run')
        elif fault == 'closed':
            broker.peer = SimpleNamespace(closed=True)
        if fault in ('owner', 'run', 'closed', 'owner-after', 'run-after', 'guard-after'):
            broker.reconcile_late_applications()
            await until(lambda: not broker.application_tasks)
            broker.prune_applications()
            assert not broker.application_records and not attempts
            assert _decision(c, decision['decisionId'])['status'] == 'outcome_unknown'
        else:
            assert (await ownership_read(c, view)).status_code == 200
            await until(lambda: not broker.application_tasks)
            if fault in ('publication', 'publication-ack'):
                assert _decision(c, decision['decisionId'])['status'] == ('outcome_unknown' if fault == 'publication' else 'denied')
                assert len(broker.application_records) == 1
                assert (await ownership_read(c, view)).status_code == 200
            await until(lambda: _decision(c, decision['decisionId'])['status'] == 'denied')
            assert len(attempts) == (2 if fault in ('publication', 'publication-ack') else 1)
            assert not broker.application_records
        with c.db() as db:
            assert db.get(RelayOperation, identifier).receipt['state'] == ('accepted' if fault in ('none', 'publication', 'publication-ack') else 'uncertain')
            assert db.query(RelayOperation).filter(RelayOperation.registration_key == 'expired-original-answer').count() == 1
        assert len(answers) == 1 and len(c.owned_factory.owners) == 1
    finally:
        broker.owners.current, broker.spec, broker.peer = owner, spec, peer
        broker.guard_check = original_guard


def test_retained_application_capacity_is_bounded_and_old_owner_releases_it():
    from acc.broker.relay_ownership import ExclusiveRuntime
    broker = object.__new__(ExclusiveRuntime)
    owner = object()
    broker.owners = SimpleNamespace(current=owner)
    broker.peer, broker.spec = SimpleNamespace(closed=False), SimpleNamespace(run_id='original')
    broker.application_records = {str(i): ({'operation': {'runId': 'original'}}, owner, None) for i in range(128)}
    broker.application_tasks, broker.application_inflight = set(), set()
    broker.observe_later({'operation': {'operationId': 'new-unresolved', 'runId': 'original'}}, owner, None)
    assert len(broker.application_records) == 128 and not broker.application_tasks
    broker.owners.current = object()
    broker.prune_applications()
    assert not broker.application_records
