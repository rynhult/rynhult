"""Dormant public L1 native-hook answers, real HTTP/CAS and fake CLI only."""
import asyncio

import pytest
from test_session_relay_claude_agent import until
from test_session_relay_claude_public import SID, mutate, public_claude  # noqa: F401
from test_session_relay_ownership import _decision, ownership_case  # noqa: F401
from test_session_relay_public_writer import attach, call
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.server import auth
from acc.server.db import write_intent
from acc.server.models import RelayDecision, RelayOperation, RelaySession
from acc.session_relay import protocol as p
from acc.session_relay.claude_hook import exchange


def qualify(c, monkeypatch, *, decisions=True):
    c.enable()
    monkeypatch.setattr('acc.server.session_relay.claude_public.PUBLIC_CLAUDE_DECISIONS_QUALIFIED', decisions)
    @write_intent
    def update():
        with c.db() as db:
            row = db.get(RelaySession, SID)
            row.capabilities = {**row.capabilities, 'decision.tool_approval': 'qualified'}
            db.commit()
    update()


async def pending_hook(c):
    pretool = {'session_id': 'fixture-conversation', 'prompt_id': 'fixture-prompt',
        'hook_event_name': 'PreToolUse', 'tool_name': 'Bash',
        'tool_input': {'command': 'echo artificial'}, 'tool_use_id': 'fixture-public-tool'}
    await asyncio.to_thread(exchange, c.owned_factory.directory / 'owner-channel.json', pretool)
    payload = {'prompt_id': 'fixture-prompt', 'session_id': 'fixture-conversation', 'hook_event_name': 'PermissionRequest',
        'tool_name': 'Bash', 'tool_input': {'command': 'echo artificial'}, 'tool_use_id': 'fixture-public-tool'}
    task = asyncio.create_task(asyncio.to_thread(exchange, c.owned_factory.directory / 'owner-channel.json', payload))
    await until(lambda: bool(c.owned_factory.hooks.pending))
    identifier = next(iter(c.owned_factory.hooks.pending))
    await until(lambda: _decision(c, identifier) is not None)
    return task, _decision(c, identifier)


async def test_claude_public_decisions_stay_unavailable_despite_fabricated_capability(public_claude, monkeypatch):  # noqa: F811
    c = public_claude
    qualify(c, monkeypatch, decisions=False)
    await attach(c.client, 'unqualified-decisions', ['decisions'])
    payload = {'sessionId': SID, 'expectedRunId': str(c.launch.run_id), 'decisionId': 'fake',
        'choiceId': 'decline', 'expectedRevision': 0, 'actionDigest': p.sha256_digest(b'fake')}
    reply = await call(c.client, 'operation.register', {'expectedRunId': str(c.launch.run_id),
        'registrationKey': 'unqualified-answer', 'method': 'decision.reply',
        'payloadDigest': p.operation_digest('decision.reply', payload)})
    assert reply['error']['data']['code'] == 'UNSUPPORTED'
    assert not c.launch.answers


@pytest.mark.parametrize('feature_off', [False, True])
async def test_public_native_hook_decline_is_one_reserved_answer_with_truthful_unknown_receipt(public_claude, monkeypatch, feature_off):  # noqa: F811
    c = public_claude
    qualify(c, monkeypatch)
    view = await attach(c.client, 'public-decision', ['decisions'])
    task, decision = await pending_hook(c)
    if feature_off:
        c.ctx.settings.feature_overrides['session_relay'] = False
    reply, identifier = await mutate(c, 'decision.reply', {'decisionId': decision['decisionId'], 'choiceId': 'decline',
        'expectedRevision': decision['revision'], 'actionDigest': decision['actionDigest']}, 'public-decline')
    assert reply['result'] == {'outcome': 'unknown'}
    assert await task == {'behavior': 'deny'}
    with c.db() as db:
        receipt = db.get(RelayOperation, identifier).receipt
        assert receipt['state'] == 'uncertain' and receipt['actor']['principal'] == 'alex'
        assert db.get(RelayDecision, (SID, decision['decisionId'])).decision['status'] == 'outcome_unknown'
    if not feature_off:
        response = await c.client.get(f'/api/sessions/{SID}/relay/ownership', params={
            'attachmentId': view['attachmentId'], 'expectedRunId': str(c.launch.run_id)})
        assert response.status_code == 200, response.text
        assert response.json()['selector']['idle_verified'] is False
        assert 'pending_decision' in response.json()['selector']['blockers']
    duplicate = await call(c.client, 'decision.reply', {'expectedRunId': str(c.launch.run_id),
        'decisionId': decision['decisionId'], 'choiceId': 'decline', 'expectedRevision': decision['revision'],
        'actionDigest': decision['actionDigest'], 'operationId': identifier})
    assert 'error' in duplicate and not c.launch.answers


async def test_claude_public_answer_reauthenticates_at_native_release(public_claude, monkeypatch):  # noqa: F811
    c = public_claude
    qualify(c, monkeypatch)
    await attach(c.client, 'revoked-public-decision', ['decisions'])
    task, decision = await pending_hook(c)
    original = c.launch.peer.handle
    seen = False
    async def revoke(method, params):
        nonlocal seen
        if method == 'authority.answer_verify':
            seen = True
            @write_intent
            def end():
                with c.db() as db:
                    auth.end_session(db, c.token)
                    db.commit()
            end()
        return await original(method, params)
    c.launch.peer.handle = revoke
    reply, identifier = await mutate(c, 'decision.reply', {'decisionId': decision['decisionId'], 'choiceId': 'accept',
        'expectedRevision': decision['revision'], 'actionDigest': decision['actionDigest']}, 'revoked-answer', status=401)
    assert seen and 'result' not in reply
    assert not c.owned_factory.hooks.pending[decision['decisionId']]['claimed']
    with c.db() as db:
        assert db.get(RelayOperation, identifier).receipt['state'] == 'uncertain'
    await c.owned_factory.hooks.close()
    assert await task == {'behavior': 'deny'}


@pytest.mark.parametrize('fault', ['none', 'reference', 'marker', 'digest', 'effect', 'missing', 'lost-reply'])
async def test_exact_broker_application_evidence_settles_only_the_original_denial(public_claude, monkeypatch, fault):  # noqa: F811
    # Explicit fake application reader: tests main's binding, not native qualification.
    c = public_claude
    qualify(c, monkeypatch)
    await attach(c.client, 'application-evidence', ['decisions'])
    def prepare(payload, identifier):
        return lambda: 'fixture-conversation:user:provider:' + identifier + ':fixture-public-tool'
    c.owned_factory.hooks.prepare_application = prepare
    task, decision = await pending_hook(c)
    original = c.owned_factory.hooks.answer
    async def observed(*args):
        result = await original(*args)
        assert result['outcome'] == 'accepted'
        if fault == 'reference':
            result['application']['nativeRef'] = 'other-conversation:user:provider:' + decision['decisionId'] + ':tool'
        elif fault == 'marker':
            result['application']['nativeRef'] = 'fixture-conversation:user:provider:wrong-marker:tool'
        elif fault == 'digest':
            result['application']['actionDigest'] = p.sha256_digest(b'another action')
        elif fault == 'effect':
            result['application']['effect'] = 'allow_once'
        elif fault == 'missing':
            result.pop('application')
        return result
    c.owned_factory.hooks.answer = observed
    if fault == 'lost-reply':
        original_call = c.launch.peer.call
        async def lost(method, params):
            value = await original_call(method, params)
            if method == 'owner.answer':
                raise ConnectionError('Artificial lost application reply')
            return value
        c.launch.peer.call = lost
    reply, identifier = await mutate(c, 'decision.reply', {'decisionId': decision['decisionId'], 'choiceId': 'decline',
        'expectedRevision': decision['revision'], 'actionDigest': decision['actionDigest']}, 'application-decline')
    if fault == 'lost-reply':
        assert reply['result']['outcome'] in ('accepted', 'unknown')
        await until(lambda: _decision(c, decision['decisionId'])['status'] == 'denied')
    else:
        assert reply['result'] == {'outcome': 'accepted' if fault == 'none' else 'unknown'}
    assert await task == {'behavior': 'deny', 'applicationMarker': decision['decisionId']}
    with c.db() as db:
        receipt = db.get(RelayOperation, identifier).receipt
        assert receipt['state'] == ('accepted' if fault in ('none', 'lost-reply') else 'uncertain')
        assert db.get(RelayDecision, (SID, decision['decisionId'])).decision['status'] == ('denied' if fault in ('none', 'lost-reply') else 'outcome_unknown')
        # The private correlation marker/reference is not a public receipt field.
        assert 'nativeRef' not in str(receipt)
    duplicate = await call(c.client, 'decision.reply', {'expectedRunId': str(c.launch.run_id),
        'decisionId': decision['decisionId'], 'choiceId': 'decline', 'expectedRevision': decision['revision'],
        'actionDigest': decision['actionDigest'], 'operationId': identifier})
    assert 'error' in duplicate


async def test_cancelled_public_answer_persists_unknown_without_reserving_again(public_claude, monkeypatch):  # noqa: F811
    c = public_claude
    qualify(c, monkeypatch)
    await attach(c.client, 'cancelled-native-answer', ['decisions'])
    hook, decision = await pending_hook(c)
    started = asyncio.Event()
    original = c.launch.peer.call
    async def held(method, params):
        if method == 'owner.answer':
            started.set()
            await asyncio.Event().wait()
        return await original(method, params)
    c.launch.peer.call = held
    answer = asyncio.create_task(mutate(c, 'decision.reply', {'decisionId': decision['decisionId'], 'choiceId': 'decline',
        'expectedRevision': decision['revision'], 'actionDigest': decision['actionDigest']}, 'cancelled-answer'))
    try:
        await asyncio.wait_for(started.wait(), 3)
        answer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await answer
        with c.db() as db:
            rows = list(db.query(RelayOperation).filter(RelayOperation.registration_key == 'cancelled-answer'))
            assert len(rows) == 1 and rows[0].receipt['state'] == 'uncertain'
            assert db.get(RelayDecision, (SID, decision['decisionId'])).decision['status'] == 'outcome_unknown'
        assert not c.launch.answers
    finally:
        hook.cancel()
        await asyncio.gather(hook, return_exceptions=True)


async def test_exact_allow_application_settles_answered_without_permission_updates(public_claude, monkeypatch):  # noqa: F811
    c = public_claude
    qualify(c, monkeypatch)
    await attach(c.client, 'exact-allow', ['decisions'])
    c.owned_factory.hooks.prepare_application = lambda payload, identifier: {'allow_once': lambda:
        'fixture-conversation:user:provider:' + identifier + ':fixture-public-tool'}
    task, decision = await pending_hook(c)
    reply, identifier = await mutate(c, 'decision.reply', {'decisionId': decision['decisionId'], 'choiceId': 'accept',
        'expectedRevision': decision['revision'], 'actionDigest': decision['actionDigest']}, 'one-allow')
    assert reply['result'] == {'outcome': 'accepted'}
    assert await task == {'behavior': 'allow'}
    with c.db() as db:
        assert db.get(RelayOperation, identifier).receipt['state'] == 'accepted'
        record = db.get(RelayDecision, (SID, decision['decisionId'])).decision
        assert record['status'] == 'answered' and record['choiceId'] == 'accept'
    assert 'error' in await call(c.client, 'decision.reply', {'expectedRunId': str(c.launch.run_id),
        'decisionId': decision['decisionId'], 'choiceId': 'accept', 'expectedRevision': decision['revision'],
        'actionDigest': decision['actionDigest'], 'operationId': identifier})


async def test_late_original_application_is_observed_without_a_second_answer(public_claude, monkeypatch):  # noqa: F811
    import threading
    c = public_claude
    qualify(c, monkeypatch)
    await attach(c.client, 'late-original', ['decisions'])
    ready = threading.Event()
    replies = []
    def prepare(payload, identifier):
        def observe():
            if not ready.is_set():
                raise p.RelayError(p.ErrorCode.BUSY)
            return 'fixture-conversation:user:provider:' + identifier + ':fixture-tool'
        return observe
    hooks = c.owned_factory.hooks
    hooks.prepare_application = prepare
    original = hooks.answer
    async def once(*args):
        replies.append(args[:3])
        return await original(*args)
    hooks.answer = once
    task, decision = await pending_hook(c)
    reply, identifier = await mutate(c, 'decision.reply', {'decisionId': decision['decisionId'], 'choiceId': 'decline',
        'expectedRevision': decision['revision'], 'actionDigest': decision['actionDigest']}, 'late-original-answer')
    assert reply['result'] == {'outcome': 'unknown'}
    assert await task == {'behavior': 'deny', 'applicationMarker': decision['decisionId']}
    ready.set()
    await until(lambda: _decision(c, decision['decisionId'])['status'] == 'denied')
    with c.db() as db:
        assert db.get(RelayOperation, identifier).receipt['state'] == 'accepted'
        assert db.query(RelayOperation).filter(RelayOperation.registration_key == 'late-original-answer').count() == 1
    assert len(replies) == 1 and len(c.owned_factory.owners) == 1
    for operation_id, run_id in [(identifier, 'wrong-run'), ('unregistered-answer', str(c.launch.run_id))]:
        with pytest.raises(p.RelayError):
            c.launch.publication({'kind': 'decision.applied', 'operationId': operation_id,
                'application': {}, 'runId': run_id})
