"""A cancelled undelivered key intent cannot consume a later explicit review."""
import asyncio

import httpx
import pytest
from test_session_relay_public_writer import SID, attach, call, public_case  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401
from test_session_relay_stock_approval_frames import approval_case, key  # noqa: F401
from test_session_relay_terminal_grants import stream_case  # noqa: F401

from acc.server.db import write_in_thread
from acc.server.models import RelayDecision, RelayOperation
from acc.server.session_relay import native_decisions
from acc.session_relay import protocol as p


@pytest.mark.parametrize('withdrawal', ['before-review', 'during-stage'])
async def test_cancelled_decoded_key_leaves_one_fresh_explicit_review(approval_case, withdrawal):  # noqa: F811
    c = approval_case
    assert 'result' in await call(c.client, 'terminal.input', key(c))
    ticket = c.approvals.submission
    assert not c.replies
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url='http://fixture') as reviewer:
        await attach(reviewer, 'cancel-then-explicit-review', ['queue', 'decisions'])
        original = (await call(reviewer, 'operation.get', {'operationId': ticket.operation_id}))['result']
        cancelled = (await call(reviewer, 'operation.cancel', {'operationId': ticket.operation_id,
            'expectedRevision': original['revision']}))['result']
        assert cancelled['state'] == 'failed' and cancelled['reason'] == 'cancelled'
        with pytest.raises(p.RelayError) as refusal:
            await c.approvals.reply(c.key_grant['inputGrant'], 1, c.offer['alias'], 'accept')
        assert refusal.value.code is p.ErrorCode.DECISION_RESOLVED
        assert not c.replies
        params = {**c.approvals.params(ticket), 'choiceId': 'cancel'}
        entered, release = asyncio.Event(), asyncio.Event()
        original_call = c.launch.peer.call
        async def stage(method, value):
            if method == 'stage' and withdrawal == 'during-stage':
                entered.set()
                await release.wait()
            return await original_call(method, value)
        c.launch.peer.call = stage
        if withdrawal == 'before-review':
            await c.approvals.resolved('key-question', None)
        registered = await call(reviewer, 'operation.register', {'expectedRunId': str(c.launch.run_id),
            'registrationKey': 'fresh-review-after-cancelled-key', 'method': 'decision.reply',
            'payloadDigest': p.operation_digest('decision.reply', params)})
        fresh = registered['result']['operationId']
        task = asyncio.create_task(call(reviewer, 'decision.reply', {**params, 'operationId': fresh}))
        try:
            if withdrawal == 'during-stage':
                await asyncio.wait_for(entered.wait(), 3)
                await c.approvals.resolved('key-question', None)
        finally:
            release.set()
        receipt = (await asyncio.wait_for(task, 5))['result']
        assert receipt['state'] == 'uncertain'
        assert c.replies == [(7, {'decision': 'cancel'})]
        assert await c.launch.handle('publication', {'kind': 'decision.applied', 'decisionId': 'key-question',
            'operationId': fresh, 'choiceId': 'cancel', 'actionDigest': c.digest,
            'nativeRef': p.sha256_digest(b'exact synthetic chosen cancel application')})
        with c.db() as db:
            assert db.get(RelayOperation, ticket.operation_id).receipt == cancelled
            assert db.get(RelayOperation, fresh).receipt['state'] == 'accepted'
            assert db.get(RelayDecision, (SID, 'key-question')).decision['status'] == 'denied'
        assert c.launch.terminal_grants.record.processed == 0
        assert c.approvals.offer is None


async def test_decoded_stock_key_keeps_terminal_origin_in_authenticated_grant(approval_case):  # noqa: F811
    c = approval_case
    assert 'result' in await call(c.client, 'terminal.input', key(c))
    resolve, params = await write_in_thread(lambda: c.approvals.begin_reply(
        c.key_grant['inputGrant'], 1, c.offer['alias'], 'accept'))
    token = await write_in_thread(lambda: native_decisions.issue(c.launch, resolve, params))
    wire = await write_in_thread(lambda: c.launch.resolve_grant(token))
    assert wire['origin'] == 'terminal'
    assert wire['actor'] == p.to_wire(c.launch.grants[token].operation.actor)
