"""Native advertised/discovered tool boundary; no invented approval family."""
import asyncio
import json
from pathlib import Path

from enrolled_decisions import prepare as prepare_decisions

from acc.server.models import RelayDecision, RelayOperation, RelaySession
from acc.server.session_relay.hub import Hub
from acc.session_relay import protocol as p


class Case:
    def __init__(self,base,tool):self.base,self.tool=base,tool
    def __getattr__(self,key):return getattr(self.base,key)
    async def exercise(self,c,state,launch,manager,config):
        environment=state.owners.environment(state.spec)
        assert Path(environment['HOME'])==self.base.root/'home'
        assert Path(environment['CLAUDE_CONFIG_DIR'])==self.base.root/'home'/'.claude'
        adapter=state.owners.current;wire=adapter.client._transport;sid=launch.session_id
        fields={'sessionId':sid,'expectedRunId':str(launch.run_id),'text':'Discover pinned supported tools',
            'expectedConfigurationRevision':0,'expectedControlRevision':0,'ifBusy':'reject'}
        op=Hub(c.ctx).register(c.resolve,sid,str(launch.run_id),'tool-discovery','turn.submit',p.operation_digest('turn.submit',fields))
        await manager.submit(c.resolve,sid,{**fields,'operationId':op.operation_id})
        async with asyncio.timeout(20):
            while True:
                with c.db() as db:
                    accepted=db.get(RelayOperation,op.operation_id).receipt['state']=='accepted'
                    completed=db.get(RelaySession,sid).active_operation_id is None
                if accepted and completed and not adapter.active and self.base.calls>=2:break
                await asyncio.sleep(.02)
        with c.db() as db:
            assert db.get(RelayOperation,op.operation_id).receipt['state']=='accepted'
            assert db.query(RelayDecision).filter_by(session_id=sid).count()==0
        assert not adapter.pending and not wire.requests and not wire.responses
        denied=await adapter._permission(self.tool,{},None)
        assert type(denied).__name__=='PermissionResultDeny' and not adapter.pending
        assert self.tool not in self.base.advertised and self.base.discovery=='No matching deferred tools found'
        Path('/evidence/result.json').write_text(json.dumps({'provider':'claude','tool':self.tool,
            'advertised':self.base.advertised,'discovery':self.base.discovery,'noNativeQuestion':True,
            'unknownCallbackRefused':True,'noChosenAnswerOrResponse':True,'modelCalls':self.base.calls,
            'terminalMediated':False,'scope':'Pinned configured API-key SDK tool advertisement and real native ToolSearch; unavailable tool callbacks refuse before publication. No approval application claim for absent tools.'}))


async def prepare_unoffered(root,work,c,tool):
    (root/'home').mkdir(mode=0o700)
    original=await prepare_decisions(root,work,c);base=original.base
    emit=base.claude_events;base.advertised=[];base.discovery=None
    def events(payload):
        if base.calls==1:base.advertised=[v['name'] for v in payload['tools']]
        if base.calls==2:
            results=[b for m in payload.get('messages',[]) for b in (m.get('content',[]) if isinstance(m.get('content'),list) else []) if b.get('tool_use_id')=='discovery-query']
            assert len(results)==1
            base.discovery=results[0].get('content')
        values=emit(payload)
        if base.calls==1:
            for name,value in values:
                if name=='content_block_start':value['content_block'].update(name='ToolSearch',id='discovery-query')
                elif name=='content_block_delta':value['delta']['partial_json']=json.dumps({'query':'select:'+tool})
        return values
    base.claude_events=events
    return Case(base,tool)
