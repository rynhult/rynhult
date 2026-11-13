"""Main settles only the original reserved winner; no fresh execution grant."""
import httpx
import pytest
from test_session_relay_public_decisions import decision_case  # noqa: F401
from test_session_relay_public_writer import SID, attach, call, public_case  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.server.models import RelayDecision, RelayOperation
from acc.server.session_relay.native_decisions import applied, closed
from acc.session_relay import protocol as p


@pytest.fixture
async def chosen(decision_case):  # noqa: F811
    c=decision_case
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app),base_url='http://fixture') as client:
        await attach(client,'applied-evidence',['decisions'])
        registration=await call(client,'operation.register',{'expectedRunId':str(c.launch.run_id),
            'registrationKey':'applied-answer','method':'decision.reply',
            'payloadDigest':p.operation_digest('decision.reply',c.answer_payload)})
        c.answer_id=registration['result']['operationId']
        result=await call(client,'decision.reply',{**c.answer_payload,'operationId':c.answer_id})
        assert result['result']['state']=='uncertain'
    c.proof={'kind':'decision.applied','decisionId':'fixture-decision','operationId':c.answer_id,
        'choiceId':'cancel','actionDigest':c.answer_payload['actionDigest'],'nativeRef':p.sha256_digest(b'qualified fixture lifecycle')}
    return c


async def test_exact_late_evidence_settles_original_receipt_after_person_authority_is_gone_without_replay(chosen):
    c=chosen;original=c.launch.resolve
    def revoked():raise AssertionError('Application evidence must not reuse person authority')
    c.launch.resolve=revoked
    try:
        assert applied(c.launch,c.proof) and applied(c.launch,c.proof)
    finally:c.launch.resolve=original
    with c.db() as db:
        receipt=db.get(RelayOperation,c.answer_id).receipt
        decision=db.get(RelayDecision,(SID,'fixture-decision')).decision
        assert receipt['state']=='accepted' and decision['status']=='denied'
        assert receipt['actor']==decision['winner']
    assert c.native_replies==[(0,{'decision':'cancel'})]


@pytest.mark.parametrize('changes',[{'choiceId':'accept'},{'actionDigest':p.sha256_digest(b'foreign')},
    {'nativeRef':'not-proof'},{'decisionId':'foreign'},{'operationId':'foreign'},{'kind':'other'}])
async def test_unmatched_evidence_cannot_settle_an_uncertain_answer(chosen,changes):
    c=chosen
    with pytest.raises(p.RelayError):applied(c.launch,{**c.proof,**changes})
    with c.db() as db:
        assert db.get(RelayOperation,c.answer_id).receipt['state']=='uncertain'
        assert db.get(RelayDecision,(SID,'fixture-decision')).decision['status']=='outcome_unknown'
    assert c.native_replies==[(0,{'decision':'cancel'})]


async def test_cleanup_cannot_cancel_a_chosen_unknown_answer(chosen):
    c=chosen
    assert not closed(c.launch,{'kind':'decision.closed','decisionId':'fixture-decision',
        'actionDigest':c.answer_payload['actionDigest']})
    with c.db() as db:
        assert db.get(RelayOperation,c.answer_id).receipt['state']=='uncertain'
        assert db.get(RelayDecision,(SID,'fixture-decision')).decision['status']=='outcome_unknown'


async def test_durable_original_operation_scope_and_reference_are_immutable_even_on_duplicate(chosen):
    c=chosen
    with c.db() as db:
        receipt=db.get(RelayOperation,c.answer_id).receipt
        original={key:receipt[key] for key in ('operationId','runId','method','actor','payloadDigest')}
    assert applied(c.launch,c.proof,original_operation=original)
    assert applied(c.launch,c.proof,original_operation=original)
    for change in ({'payloadDigest':p.sha256_digest(b'foreign')},{'actor':{**original['actor'],'principal':'foreign'}}):
        with pytest.raises(p.RelayError):applied(c.launch,c.proof,original_operation={**original,**change})
    with pytest.raises(p.RelayError):applied(c.launch,{**c.proof,'nativeRef':p.sha256_digest(b'foreign')},original_operation=original)
