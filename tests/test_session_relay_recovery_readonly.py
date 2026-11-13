"""R1 sealed-session records remain readable over real cookie HTTP; fake owner only."""
from test_session_relay_ownership import SID, ownership_case  # noqa: F401
from test_session_relay_public_l1 import public_l1  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.server.db import write_intent
from acc.server.models import RelayOperation, RelayOwnership, RelaySession
from acc.server.session_relay import history, store
from acc.server.session_relay.maintenance import recover
from acc.session_relay import protocol as p


async def test_r1_sealed_recovery_keeps_original_records_readable_without_reenrollment(public_l1):  # noqa: F811
    c=public_l1
    @write_intent
    def qualify_fake_text():
        with c.db() as db:
            row=db.get(RelaySession,SID)
            row.capabilities={**row.capabilities,'text':'qualified'}
            db.commit()
    qualify_fake_text()
    fields={'sessionId':SID,'expectedRunId':str(c.launch.run_id),'text':'Do not replay this task',
        'expectedConfigurationRevision':0,'ifBusy':'queue'}
    registered=await c.public.call('operation.register',{'expectedRunId':str(c.launch.run_id),
        'registrationKey':'before-recovery','method':'turn.submit','payloadDigest':p.operation_digest('turn.submit',fields)})
    assert 'result' in registered,registered
    operation=registered['result']['operationId']
    @write_intent
    def seed():
        with c.db() as db:
            row=store.lock(db,SID)
            history.message(db,row,item_id='old-output',role='assistant',complete_text='Keep this old output',
                source_epoch=1,source_revision=1,mask=lambda value:value)
            store.add_decision(db,row,p.Decision('old-question',SID,str(row.current_run_id),p.DecisionKind.QUESTION,
                p.sha256_digest(b'old-question'),'Keep this old decision',
                (p.DecisionChoice('yes','Yes',p.ChoiceEffect.ANSWER),),9999999999999,0))
            db.commit()
    seed()
    count=len(c.owned_factory.owners)
    # Exercise production coordinator fencing with no enrolled current manager.
    # This is a fake-owner HTTP contract test; actual process stops are separate.
    recover(c.ctx)
    response=await c.public.client.get(f'/api/sessions/{SID}/relay')
    assert response.status_code==200,response.text
    value=response.json()
    assert value['l1']['phase']=='recovery_required' and value['l1']['recoveryMode']=='new_session'
    assert value['l1']['recoveryAvailable'] and not value['l1']['available']
    receipt=next(item for item in value['receipts'] if item['operationId']==operation)
    assert receipt['state']=='failed' and receipt['payloadDigest']==p.operation_digest('turn.submit',fields)
    decision=next(item for item in value['decisionOutcomes'] if item['decisionId']=='old-question')
    assert decision['description']=='Keep this old decision' and decision['status']=='cancelled'
    events=await c.public.client.get(f'/api/sessions/{SID}/relay/events')
    assert events.status_code==200,events.text
    assert any(item['kind']=='message.upsert' and item['payload']['text']=='Keep this old output'
        for item in events.json()['events'])
    attempt=await c.public.call('turn.submit',{**fields,'operationId':operation})
    assert 'error' in attempt,attempt
    with c.db() as db:
        assert db.get(RelaySession,SID).sealed
        assert db.get(RelayOwnership,SID).phase=='recovery_required'
        assert db.get(RelayOperation,operation).receipt==receipt
    assert len(c.owned_factory.owners)==count
