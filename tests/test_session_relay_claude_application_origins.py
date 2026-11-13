"""Disk-correlated original answers remain evidence-only after a cache miss."""
import threading
from copy import copy

import pytest
from test_session_relay_claude_agent import until
from test_session_relay_claude_public import mutate, public_claude  # noqa: F401
from test_session_relay_claude_public_decisions import pending_hook, qualify
from test_session_relay_ownership import SID, _decision, ownership_case  # noqa: F401
from test_session_relay_public_writer import attach
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.server.db import make_engine, make_sessionmaker, write_intent
from acc.server.models import RelayOperation, RelayRun
from acc.server.session_relay import application_origins
from acc.session_relay import protocol as p


@pytest.mark.parametrize('fault', ['none', 'fresh-engine', 'missing', 'actor', 'digest', 'params', 'binding', 'identity', 'malformed'])
async def test_unknown_original_application_recovers_only_exact_disk_origin(public_claude, monkeypatch, fault):  # noqa: F811
    c = public_claude
    qualify(c, monkeypatch)
    monkeypatch.setattr('acc.broker.relay_ownership.APPLICATION_OBSERVATION_SECONDS', 0)
    await attach(c.client, 'disk-origin-reader', ['decisions', 'switch'])
    ready = threading.Event()
    hooks = c.owned_factory.hooks
    def prepare(payload, identifier):
        def proof():
            if not ready.is_set():
                raise p.RelayError(p.ErrorCode.BUSY)
            return 'fixture-conversation:user:provider:' + identifier + ':fixture-tool'
        return proof
    hooks.prepare_application = prepare
    native_answers = []
    original_answer = hooks.answer
    async def counted(*args):
        native_answers.append(True)
        return await original_answer(*args)
    hooks.answer = counted
    pending, decision = await pending_hook(c)
    response, identifier = await mutate(c, 'decision.reply', {'decisionId': decision['decisionId'], 'choiceId': 'decline',
        'expectedRevision': decision['revision'], 'actionDigest': decision['actionDigest']}, 'disk-original-answer')
    assert response['result'] == {'outcome': 'unknown'}
    await pending
    await until(lambda: not c.broker_state.application_tasks)
    original = dict(c.launch.answer_operations[identifier])
    c.launch.answer_operations.clear()
    engine, original_context = None, c.launch.ctx
    if fault == 'fresh-engine':
        engine = make_engine(str(c.engine.url))
        context = copy(original_context)
        context.db = make_sessionmaker(engine)
        c.launch.ctx = context
    @write_intent
    def change():
        with c.db() as db:
            record = db.get(RelayRun, c.launch.run_id)
            origins = record.identity[application_origins.KEY]
            saved = copy(origins[identifier])
            if fault == 'missing':
                origins = {}
            elif fault == 'actor':
                saved['actor'] = {**saved['actor'], 'principal': 'wrong-person'}
            elif fault == 'digest':
                saved['payloadDigest'] = p.sha256_digest(b'wrong')
            elif fault == 'params':
                saved['params'] = {**saved['params'], 'choiceId': 'accept'}
            elif fault == 'binding':
                saved['bindingId'] = 'wrong-binding'
            elif fault == 'identity':
                record.identity = {**record.identity, 'processIdentity': 'wrong-process'}
            elif fault == 'malformed':
                saved = {'params': {'operationId': identifier}}
            if fault != 'missing':
                origins = {**origins, identifier: saved}
            record.identity = {**record.identity, application_origins.KEY: origins}
            db.commit()
    try:
        if fault not in ('none', 'fresh-engine'):
            change()
            with pytest.raises(p.RelayError):
                application_origins.load(c.launch, identifier)
        else:
            assert application_origins.load(c.launch, identifier) == original
        ready.set()
        c.broker_state.reconcile_late_applications()
        await until(lambda: not c.broker_state.application_tasks)
        expected = 'denied' if fault in ('none', 'fresh-engine') else 'outcome_unknown'
        assert _decision(c, decision['decisionId'])['status'] == expected
        with c.db() as db:
            op = db.get(RelayOperation, identifier)
            assert op.receipt['state'] == ('accepted' if expected == 'denied' else 'uncertain')
            assert db.query(RelayOperation).filter(RelayOperation.registration_key == 'disk-original-answer').count() == 1
        assert len(native_answers) == 1 and len(c.owned_factory.owners) == 1
    finally:
        c.launch.ctx = original_context
        if engine:
            engine.dispose()
