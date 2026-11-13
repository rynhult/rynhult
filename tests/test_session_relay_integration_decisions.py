"""Both worker qualifications retain exact public Claude registration and CAS."""
import pytest
from test_session_relay_claude_public import SID, mutate, public_claude  # noqa: F401
from test_session_relay_claude_public_decisions import pending_hook, qualify
from test_session_relay_ownership import ownership_case  # noqa: F401
from test_session_relay_public_writer import attach
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.server.db import write_intent
from acc.server.models import RelayDecision, RelayOperation, RelayRun


@pytest.mark.parametrize('primary,enrolled', [(True, False), (False, True), (True, True)])
async def test_primary_and_enrolled_decisions_each_register_and_release_one_exact_answer(public_claude, monkeypatch, primary, enrolled):  # noqa: F811
    c = public_claude
    qualify(c, monkeypatch, decisions=primary)
    monkeypatch.setattr('acc.server.session_relay.writers.PUBLIC_WRITER_QUALIFIED', enrolled)
    await attach(c.client, 'joined-decision', ['decisions'])
    pending, decision = await pending_hook(c)
    attempts = []
    original = c.owned_factory.hooks.answer
    async def answer(*args):
        attempts.append(args[:3])
        return await original(*args)
    c.owned_factory.hooks.answer = answer
    result, identifier = await mutate(c, 'decision.reply', {'decisionId': decision['decisionId'], 'choiceId': 'decline',
        'expectedRevision': decision['revision'], 'actionDigest': decision['actionDigest']}, 'joined-answer')
    assert result['result'] == {'outcome': 'unknown'} if primary else result['result']['state'] == 'uncertain'
    assert await pending == {'behavior': 'deny'}
    assert attempts == [(decision['decisionId'], decision['actionDigest'], 'deny')]
    with c.db() as db:
        receipt = db.get(RelayOperation, identifier).receipt
        assert receipt['actor']['principal'] == 'alex' and receipt['state'] == 'uncertain'
        assert db.query(RelayOperation).filter_by(registration_key='joined-answer').count() == 1
    # Cold coordinator cache recovers evidence correlation, never a new grant or
    # another permission answer. Explicit fake proof; no native qualification.
    c.launch.answer_operations.clear()
    applied = await c.launch.handle('publication', {'kind': 'decision.applied', 'operationId': identifier,
        'runId': str(c.launch.run_id), 'application': {'effect': 'deny', 'decisionId': decision['decisionId'],
            'actionDigest': decision['actionDigest'], 'nativeRef': 'fixture-conversation:user:provider:' + decision['decisionId'] + ':fixture-tool'}})
    assert applied == {'outcome': 'accepted'} and len(attempts) == 1
    with c.db() as db:
        assert db.get(RelayOperation, identifier).receipt['state'] == 'accepted'


async def test_expired_answer_fails_without_persisting_an_application_origin(public_claude, monkeypatch):  # noqa: F811
    c = public_claude
    qualify(c, monkeypatch)
    await attach(c.client, 'expired-origin', ['decisions'])
    pending, decision = await pending_hook(c)
    @write_intent
    def expire():
        with c.db() as db:
            row = db.get(RelayDecision, (SID, decision['decisionId']))
            row.decision = {**row.decision, 'expiresAtMs': 1}
            db.commit()
    expire()
    try:
        result, identifier = await mutate(c, 'decision.reply', {'decisionId': decision['decisionId'], 'choiceId': 'decline',
            'expectedRevision': decision['revision'], 'actionDigest': decision['actionDigest']}, 'expired-no-origin')
        assert result['error']['data']['code'] == 'DECISION_EXPIRED'
        with c.db() as db:
            receipt = db.get(RelayOperation, identifier).receipt
            assert receipt['state'] == 'failed' and receipt['reason'] == 'expired'
            assert identifier not in db.get(RelayRun, c.launch.run_id).identity.get('applicationOrigins', {})
        assert not c.launch.answers and not c.launch.answer_operations
    finally:
        await c.owned_factory.hooks.close()
        assert await pending == {'behavior': 'deny'}
