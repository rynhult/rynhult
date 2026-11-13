"""Native failed tool application plus lost original main publication ACK."""
import json

from enrolled_tui_allow_ack import Case as AllowCase
from enrolled_tui_allow_ack import prepare as prepare_allow


class Case(AllowCase):
    async def after_granted(self,c,state,launch,manager,owner,attached,claim,proof):
        call=state.peer.call;losses=[]
        async def lost(method,params):
            result=await call(method,params)
            if method=='publication' and params.get('kind')=='decision.applied' and not losses:
                losses.append(params['operationId']);raise ConnectionError('Fixture loses original main publication ACK')
            return result
        state.peer.call=lost
        try:await super().after_granted(c,state,launch,manager,owner,attached,claim,proof)
        finally:state.peer.call=call
        rows=state.decision_evidence.db.execute('SELECT completion,settled FROM decision_evidence').fetchall()
        assert len(rows)==1 and rows[0][1]==1 and losses
        completion=json.loads(rows[0][0]);assert completion['event']=='PostToolUseFailure',completion
        proof['allowFailure']={'nativeNonzeroExit':7,'nativeCompletionEvent':completion['event'],
            'mainPublicationAckLost':True,'sameOriginalEvidenceRetried':True,'oneNativeAnswer':True}
        proof['scope']='Failed Bash tool, native PostToolUseFailure, lost original main ACK. '+proof['scope']


async def prepare(root,work,c):
    base=await prepare_allow(root,work,c);result=Case(base.base)
    native=result.base.base;original=native.claude_events
    def events(payload):
        values=original(payload)
        if any(name=='content_block_start' and value['content_block'].get('type')=='tool_use' for name,value in values):
            for name,value in values:
                if name=='content_block_delta':
                    data=json.loads(value['delta']['partial_json']);data['command']+='; exit 7'
                    value['delta']['partial_json']=json.dumps(data)
        return values
    native.claude_events=events
    return result
