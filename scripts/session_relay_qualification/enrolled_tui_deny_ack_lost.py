"""Exact native Deny application survives lost publication acknowledgement."""
from enrolled_tui_deny_ack import Case as DenyCase
from enrolled_tui_expiry import prepare as prepare_expiry

from acc.server.models import RelayOperation


class Case(DenyCase):
    async def after_granted(self,c,state,launch,manager,owner,attached,claim,proof):
        calls=[]
        call=state.peer.call
        async def capture(method,params):
            value=await call(method,params)
            if method=='authority.answer':calls.append(value['operation'])
            return value
        state.peer.call=capture
        publish=state.publish
        answer=state.owners.hooks.answer
        writes=0
        losses=0
        async def once(*args,**kwargs):
            nonlocal writes
            writes+=1
            return await answer(*args,**kwargs)
        async def lost(event):
            nonlocal losses
            value=await publish(event)
            if event.get('kind')=='decision.applied' and losses==0:
                losses+=1
                raise ConnectionError('Fixture drops acknowledgement after original native application settlement')
            return value
        state.publish=lost
        state.owners.hooks.answer=once
        await super().after_granted(c,state,launch,manager,owner,attached,claim,proof)
        assert losses==1 and writes==1 and len(calls)==1
        original=calls[0]
        with c.db() as db:
            receipt=db.get(RelayOperation,original['operationId']).receipt
            assert receipt['state']=='accepted' and receipt['actor']==original['actor']
            assert receipt['payloadDigest']==original['payloadDigest']
        proof['originalDecisionReceipt']=receipt
        proof.update(lostApplicationPublicationAck=True,oneNativeAnswer=True,
            scope=proof['scope']+' Actual publication acknowledgement lost after durable original settlement; accepted receipt and no second native answer.')


async def prepare(root,work,c):
    from acc.broker import claude_denial_evidence
    claude_denial_evidence.CLAUDE_TUI_DENY_ACK_QUALIFIED=True
    expiry=await prepare_expiry(root,work,c)
    return Case(expiry.base)
