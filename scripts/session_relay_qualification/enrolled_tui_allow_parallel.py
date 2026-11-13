"""Two actual native tool questions, distinct IDs or ambiguous identical inputs."""
import asyncio
import base64
import copy
import json
from pathlib import Path

from enrolled_tui_allow_tools import prepare_mode as prepare_read
from enrolled_tui_expiry import QUESTION
from execution_crash_worker import Case as ExecutionCase

from acc.server.models import RelayDecision, RelayOperation
from acc.session_relay import protocol as p


class Case(ExecutionCase):
    async def exercise(self,c,state,launch,manager,config):
        self.proof={}
        try:
            await self.bootstrap(c,state,launch,manager)
            Path('/evidence/result.json').write_text(json.dumps(self.proof))
        finally:
            if hasattr(self,'public'):await self.public.close()

    async def after_granted(self,c,state,launch,manager,owner,attached,claim,proof):
        sid=state.spec.spec['session_id'];run=str(launch.run_id);answers=[]
        answer=state.owners.hooks.answer
        async def recorded(identifier,digest,effect,*args,**kwargs):
            answers.append({'decisionId':identifier,'effect':effect,
                'boundToolId':state.owners.hooks.pending[identifier]['boundToolId']})
            return await answer(identifier,digest,effect,*args,**kwargs)
        state.owners.hooks.answer=recorded
        for seq,data in ((1,QUESTION.encode()),(2,b'\r')):
            result=await self.public.call('terminal.input',{'attachmentId':self.public.view['attachmentId'],
                'inputGrant':claim.input_grant,'inputEpoch':claim.input_epoch,'frameSeq':seq,'data':base64.b64encode(data).decode()})
            assert 'result' in result,result
            if seq==1:await asyncio.sleep(.7)
        async with asyncio.timeout(20):
            while not state.owners.hooks.tool_bindings.active:await asyncio.sleep(.01)
        try:
            async with asyncio.timeout(8):
                while len(state.owners.hooks.pending)!=2:await asyncio.sleep(.01)
        except TimeoutError:
            from acc.broker import ops
            print('Parallel native diagnostic',json.dumps({'calls':self.base.calls,'pending':list(state.owners.hooks.pending),
                'active':state.owners.hooks.tool_bindings.active,'ambiguous':list(state.owners.hooks.tool_bindings.ambiguous),
                'frames':[owner.host.admitted,owner.host.processed],'offered':self.base.native_tool_offered}),flush=True)
            print(ops.tmux(state.owners.config,'capture-pane','-p','-J','-S','-100','-t','='+owner.spec.spec['tmux_name']+':').stdout[-6500:],flush=True)
            raise
        pending={k:{key:v.get(key) for key in ('boundToolId','nativeToolId','promptDigest','toolName')}
            for k,v in state.owners.hooks.pending.items()}
        async with asyncio.timeout(3):
            while True:
                snapshot=(await self.public.call('session.snapshot',{}))['result']
                cards=[v for v in snapshot['pendingDecisions'] if v['decisionId'] in pending]
                if len(cards)==2:break
                await asyncio.sleep(.01)
        assert all(v['toolName']=='Read' for v in pending.values())
        if self.identical:
            assert len({v['promptDigest'] for v in pending.values()})==1
            assert all(not v['boundToolId'] or not state.owners.hooks.tool_bindings.permits(state.owners.hooks.pending[k],state.owners.hooks.pending)
                for k,v in pending.items())
            assert all('accept' not in {choice['id'] for choice in card['choices']} for card in cards)
        else:
            assert {v['boundToolId'] for v in pending.values()}=={'installed_parallel_0','installed_parallel_1'}
            assert len({v['promptDigest'] for v in pending.values()})==2
        async def register(card,label,choice):
            fields={'sessionId':sid,'expectedRunId':run,'decisionId':card['decisionId'],'choiceId':choice,
                'expectedRevision':card['revision'],'actionDigest':card['actionDigest']}
            result=await self.public.call('operation.register',{'expectedRunId':run,'registrationKey':label,
                'method':'decision.reply','payloadDigest':p.operation_digest('decision.reply',fields)})
            assert 'result' in result,result
            return {**fields,'operationId':result['result']['operationId']}
        if self.identical:
            for i,card in enumerate(cards):
                refused=await self.public.call('decision.reply',await register(card,'ambiguous-allow-'+str(i),'accept'))
                assert 'error' in refused,refused
            assert not answers
        fields=[await register(card,'parallel-answer-'+str(i),'decline' if self.identical else 'accept')
            for i,card in enumerate(reversed(cards))]
        replies=await asyncio.gather(*(self.public.call('decision.reply',v) for v in fields))
        assert all('result' in v for v in replies),replies
        async with asyncio.timeout(20):
            while owner.host.processed!=launch.terminal_grants.record.processed or owner.host.processed!=2 or state.owners.hooks.pending:
                await asyncio.sleep(.02)
        assert len(answers)==2 and len({v['decisionId'] for v in answers})==2
        assert {v['effect'] for v in answers}==({'deny'} if self.identical else {'allow_once'})
        receipts=[]
        async with asyncio.timeout(20):
            while True:
                with c.db() as db:
                    receipts=[dict(db.get(RelayOperation,v['operationId']).receipt) for v in fields]
                if self.identical or all(v['state']=='accepted' for v in receipts):break
                await asyncio.sleep(.025)
        with c.db() as db:
            decisions=[dict(db.get(RelayDecision,(sid,v['decisionId'])).decision) for v in fields]
        assert all((self.base.root/('parallel-'+str(i))).read_text()==str(i) for i in range(2))
        if not self.identical:
            assert all(v['status']=='answered' and v['choiceId']=='accept' for v in decisions)
        before=self.base.calls
        repeated=await asyncio.gather(*(self.public.call('decision.reply',v) for v in fields))
        assert all(v.get('result',{}).get('operationId')==receipt['operationId'] and v['result']['payloadDigest']==receipt['payloadDigest'] for v,receipt in zip(repeated,receipts,strict=True))
        assert self.base.calls==before and len(answers)==2
        self.proof={'provider':'claude','twoActualNativePendingQuestions':True,'identicalInputs':self.identical,
            'pendingBindings':pending,'nativeAnswers':answers,'receipts':receipts,
            'decisions':decisions,'noAnswerReplay':True,'modelCalls':self.base.calls,
            'exactDistinctNativeIdsAllowed':not self.identical,'ambiguousAllowClearlyUnavailableAndRefused':self.identical,
            'terminalMediated':False,'scope':'Actual isolated pinned Claude TUI produces two simultaneous PermissionRequest hooks after exact PreToolUse IDs. Real public cookie/CSRF and original main CAS; distinct inputs answered concurrently in reverse card order, identical inputs refuse remote Allow and receive explicit Deny only. No subscription, stock keys, restart or other authority timing claim.'}


async def prepare_mode(root,work,c,identical):
    from acc.broker import claude_allow_evidence, claude_denial_evidence
    claude_allow_evidence.CLAUDE_TUI_ALLOW_ACK_QUALIFIED=True
    claude_denial_evidence.CLAUDE_TUI_DENY_ACK_QUALIFIED=True
    expiry=await prepare_read(root,work,c,'Read');result=Case(expiry.base);result.identical=identical
    for i in range(2):(root/('parallel-'+str(i))).write_text(str(i))
    native=result.base.base;original=native.claude_events
    def events(payload):
        values=original(payload)
        if not any(name=='content_block_start' and v['content_block'].get('type')=='tool_use' for name,v in values):return values
        block=values[1:4];out=[values[0]]
        for i in range(2):
            chunk=copy.deepcopy(block)
            for name,value in chunk:
                value['index']=i
                if name=='content_block_start':value['content_block']['id']='installed_parallel_'+str(i)
                elif name=='content_block_delta':value['delta']['partial_json']=json.dumps({'file_path':str(root/('parallel-'+str(0 if identical else i)))})
            out.extend(chunk)
        return [*out,*values[4:]]
    native.claude_events=events
    return result


async def prepare(root,work,c):return await prepare_mode(root,work,c,False)
