"""Public answer composition over real main/broker; artificial native callback."""
import asyncio
import time
from dataclasses import replace

import httpx
import pytest
from test_session_relay_public_writer import SID, attach, call, public_case  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.server.db import write_intent
from acc.server.models import RelayDecision, RelayOperation, RelaySession
from acc.session_relay import protocol as p


@pytest.fixture
async def decision_case(public_case, monkeypatch):  # noqa: F811
    c = public_case
    c.qualify_fixture()
    monkeypatch.setattr('acc.server.session_relay.writers.PUBLIC_WRITER_QUALIFIED', True)
    @write_intent
    def enable_fixture_capability():
        with c.db() as db:
            row = db.get(RelaySession, SID)
            row.capabilities = {**row.capabilities, 'decision.tool_approval': 'qualified'}
            db.commit()
    enable_fixture_capability()
    state = c.broker_state
    state.adapter.record = replace(state.adapter.record, qualification_id='synthetic-public-decisions',
        qualification_date='2026-11-12', operations={**state.adapter.record.operations,
        'decision.tool_approval': p.CapabilityEntry(p.Support.QUALIFIED, evidence=('artificial callback only',))})
    state.adapter.active_turn = 'fixture-operation'
    state.adapter.turns['fixture-operation'] = 'fixture-turn'
    expires = int(time.time() * 1000) + 60_000
    digest = p.sha256_digest(b'artificial native callback')
    state.adapter.register_decision('fixture-decision', native_request={'id': 0,
        'method': 'item/commandExecution/requestApproval', 'params': {'threadId': state.binding.native_conversation,
            'turnId': 'fixture-turn', 'itemId': 'fixture-item', 'kind': 'command', 'environmentId': 'local',
            'startedAtMs': int(time.time() * 1000), 'availableDecisions': ['accept', 'cancel']}},
        action_digest=digest, revision=0, expires_ms=expires)
    c.launch.publication({'kind': 'decision', 'decisionId': 'fixture-decision', 'actionDigest': digest,
        'expiresAtMs': expires, 'choices': ['accept', 'cancel'], 'description': 'Command: artificial fixture'})
    c.native_replies = []
    async def respond(identifier, value, *, before_write=None):
        if before_write is not None:
            await before_write()
        c.native_replies.append((identifier, value))
    state.owner.respond = respond
    c.answer_payload = {'sessionId': SID, 'expectedRunId': str(c.launch.run_id), 'decisionId': 'fixture-decision',
        'choiceId': 'cancel', 'expectedRevision': 0, 'actionDigest': digest}
    return c


async def register(client, c, key='answer', **changes):
    payload = {**c.answer_payload, **changes}
    reply = await call(client, 'operation.register', {'expectedRunId': str(c.launch.run_id), 'registrationKey': key,
        'method': 'decision.reply', 'payloadDigest': p.operation_digest('decision.reply', payload)})
    return payload, reply


async def test_public_answer_uses_hub_reservation_and_existing_uncertain_receipt_without_replay(decision_case):
    c = decision_case
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url='http://fixture') as client:
        await attach(client, 'answer-person', ['decisions'])
        payload, registration = await register(client, c)
        operation = registration['result']['operationId']
        first = await call(client, 'decision.reply', {**payload, 'operationId': operation})
        assert first['result']['state'] == 'uncertain'
        replay = await call(client, 'decision.reply', {**payload, 'operationId': operation})
        assert replay['result'] == first['result']
        with c.db() as db:
            decision = db.get(RelayDecision, (SID, 'fixture-decision')).decision
            assert decision['status'] == 'outcome_unknown' and decision['winner']['principal'] == 'alex'
        assert c.native_replies == [(0, {'decision': 'cancel'})]
        assert not c.launch.grants


async def test_rollback_only_server_held_deny_choice_can_register_and_dispatch(decision_case):
    c = decision_case
    c.ctx.settings = replace(c.ctx.settings, feature_overrides={'session_relay': False})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url='http://fixture') as client:
        await attach(client, 'rollback-person', ['decisions'])
        _, allow = await register(client, c, key='not-allow', choiceId='accept')
        assert 'error' in allow
        payload, deny = await register(client, c)
        assert 'result' in deny, deny
        reply = await call(client, 'decision.reply', {**payload, 'operationId': deny['result']['operationId']})
        assert reply['result']['state'] == 'uncertain'
    assert c.native_replies == [(0, {'decision': 'cancel'})]


@pytest.mark.parametrize('change', ['detach', 'capability', 'flag_allow'])
async def test_answer_rechecks_authority_and_effect_after_staging(decision_case, change):
    c = decision_case
    staged, release = asyncio.Event(), asyncio.Event()
    original = c.launch.peer.call
    async def paused(method, params):
        result = await original(method, params)
        if method == 'stage':
            staged.set()
            await release.wait()
        return result
    c.launch.peer.call = paused
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url='http://fixture') as client:
        view = await attach(client, 'staging-person', ['decisions'])
        payload, registered = await register(client, c, choiceId='accept' if change == 'flag_allow' else 'cancel')
        identifier = registered['result']['operationId']
        pending = asyncio.create_task(call(client, 'decision.reply', {**payload, 'operationId': identifier}))
        await asyncio.wait_for(staged.wait(), 5)
        if change == 'detach':
            await call(client, 'session.detach', {'attachmentId': view['attachmentId'], 'connectionSeq': 2})
        elif change == 'capability':
            @write_intent
            def revoke():
                with c.db() as db:
                    row = db.get(RelaySession, SID)
                    row.capabilities = {**row.capabilities, 'decision.tool_approval': 'unsupported'}
                    db.commit()
            revoke()
        else:
            c.ctx.settings = replace(c.ctx.settings, feature_overrides={'session_relay': False})
        release.set()
        assert 'error' in await asyncio.wait_for(pending, 5)
        with c.db() as db:
            assert db.get(RelayDecision, (SID, 'fixture-decision')).decision['status'] == 'pending'
            assert db.get(RelayOperation, identifier).receipt['state'] == 'failed'
    assert not c.native_replies and not c.launch.grants


async def test_invalid_registered_choice_leaves_no_queued_answer_or_winner(decision_case):
    c = decision_case
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url='http://fixture') as client:
        await attach(client, 'invalid-person', ['decisions'])
        payload, registration = await register(client, c, choiceId='acceptForSession')
        identifier = registration['result']['operationId']
        reply = await call(client, 'decision.reply', {**payload, 'operationId': identifier})
        assert 'error' in reply
        with c.db() as db:
            assert db.get(RelayDecision, (SID, 'fixture-decision')).decision['status'] == 'pending'
            assert db.get(RelayOperation, identifier).receipt['state'] == 'failed'
    assert not c.native_replies and not c.launch.grants


async def test_public_default_stays_closed_despite_fabricated_decision_capability(decision_case, monkeypatch):
    c = decision_case
    monkeypatch.setattr('acc.server.session_relay.writers.PUBLIC_WRITER_QUALIFIED', False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url='http://fixture') as client:
        await attach(client, 'unqualified-person', ['decisions'])
        _, registered = await register(client, c)
        assert registered['error']['data']['code'] == 'UNSUPPORTED'
    assert not c.native_replies and not c.launch.grants


async def test_conclusive_post_reservation_refusal_needs_fresh_review(decision_case):
    c = decision_case
    probed, release = asyncio.Event(), asyncio.Event()
    original = c.broker_state.adapter.probe
    async def paused(binding):
        result = await original(binding)
        probed.set()
        await release.wait()
        return result
    c.broker_state.adapter.probe = paused
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url='http://fixture') as client:
        view = await attach(client, 'post-reservation-person', ['decisions'])
        payload, registered = await register(client, c)
        identifier = registered['result']['operationId']
        pending = asyncio.create_task(call(client, 'decision.reply', {**payload, 'operationId': identifier}))
        await asyncio.wait_for(probed.wait(), 5)
        with c.db() as db:
            assert db.get(RelayDecision, (SID, 'fixture-decision')).decision['status'] == 'reserved'
        await call(client, 'session.detach', {'attachmentId': view['attachmentId'], 'connectionSeq': 2})
        release.set()
        assert 'error' in await asyncio.wait_for(pending, 5)
        with c.db() as db:
            receipt = db.get(RelayOperation, identifier).receipt
            assert receipt['state'] == 'failed' and receipt['reason'] == 'rejected'
            assert db.get(RelayDecision, (SID, 'fixture-decision')).decision['status'] == 'invalidated'
    assert not c.native_replies and not c.launch.grants

async def test_two_public_attachments_have_one_winner_and_no_background_redispatch(decision_case):
    c = decision_case
    transport = httpx.ASGITransport(c.app)
    async with httpx.AsyncClient(transport=transport, base_url='http://fixture') as terminal, \
            httpx.AsyncClient(transport=transport, base_url='http://fixture') as chat:
        await attach(terminal, 'terminal-review', ['decisions'])
        await attach(chat, 'chat-review', ['decisions'])
        contenders = []
        for client, key, choice in ((terminal, 'terminal-race', 'accept'), (chat, 'chat-race', 'cancel')):
            payload, registered = await register(client, c, key=key, choiceId=choice)
            assert 'result' in registered, registered
            contenders.append((client, {**payload, 'operationId': registered['result']['operationId']}))
        results = await asyncio.gather(*(call(client, 'decision.reply', params) for client, params in contenders))
        assert sum('result' in result for result in results) == 1, results
        winner = next(index for index, result in enumerate(results) if 'result' in result)
        assert results[winner]['result']['state'] == 'uncertain'
        assert c.native_replies == [(0, {'decision': contenders[winner][1]['choiceId']})]
        # A queued-turn drain must never send a decision a second time.
        await c.manager.drain(c.launch)
        assert len(c.native_replies) == 1 and not c.launch.grants
        with c.db() as db:
            decision = db.get(RelayDecision, (SID, 'fixture-decision')).decision
            assert decision['status'] == 'outcome_unknown'
            assert decision['choiceId'] == contenders[winner][1]['choiceId']
            states = [db.get(RelayOperation, params['operationId']).receipt['state'] for _, params in contenders]
            assert states[winner] == 'uncertain' and states[1 - winner] == 'failed'


@pytest.mark.parametrize('choice, allowed', [('cancel', True), ('accept', False)])
async def test_security_a4_feature_off_after_staging_permits_only_authenticated_deny(decision_case, choice, allowed):
    c = decision_case
    staged, release = asyncio.Event(), asyncio.Event()
    original = c.launch.peer.call
    async def paused(method, params):
        result = await original(method, params)
        if method == 'stage':
            staged.set()
            await release.wait()
        return result
    c.launch.peer.call = paused
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url='http://fixture') as client:
        await attach(client, 'security-a4-rollback', ['decisions'])
        payload, registered = await register(client, c, choiceId=choice)
        identifier = registered['result']['operationId']
        pending = asyncio.create_task(call(client, 'decision.reply', {**payload, 'operationId': identifier}))
        try:
            await asyncio.wait_for(staged.wait(), 5)
            c.ctx.settings = replace(c.ctx.settings, feature_overrides={'session_relay': False})
        finally:
            release.set()
        response = await asyncio.wait_for(pending, 5)
        if allowed:
            assert response['result']['state'] == 'uncertain'
            assert c.native_replies == [(0, {'decision': 'cancel'})]
        else:
            assert 'error' in response
            assert not c.native_replies
            with c.db() as db:
                assert db.get(RelayDecision, (SID, 'fixture-decision')).decision['status'] == 'pending'
                assert db.get(RelayOperation, identifier).receipt['state'] == 'failed'
    assert not c.launch.grants
