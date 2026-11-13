"""Actual owned TUI permission expiry at its original native15-second deadline."""
import asyncio
import base64
import json
import shlex
import time
from types import SimpleNamespace

from enrolled_frozen_native import Case as FrozenCase
from enrolled_frozen_native import prepare as prepare_frozen
from installed_security import Security

from acc.broker import ops
from acc.server.models import RelayDecision, RelayOperation
from acc.server.session_relay.hub import Hub
from acc.session_relay import protocol as p

QUESTION='Native unanswered permission expiry'


class Case(FrozenCase):
    async def after_granted(self,c,state,launch,manager,owner,attached,claim,proof):
        sid,run=state.spec.spec['session_id'],str(launch.run_id)
        async def frames(sequence,text):
            for seq,data in ((sequence,text.encode()),(sequence+1,b'\r')):
                await manager.input_terminal(attached,sid,{'inputGrant':claim.input_grant,'inputEpoch':claim.input_epoch,
                    'frameSeq':seq,'data':base64.b64encode(data).decode()})
                if seq==sequence:await asyncio.sleep(.7)
        await frames(3,QUESTION)
        try:
            async with asyncio.timeout(10):
                while not state.owners.hooks.pending:await asyncio.sleep(.01)
        except TimeoutError:
            print('Artificial TUI question state:',self.base.calls,self.base.native_tool_offered,
                owner.host.admitted,owner.host.processed,owner.idle,owner.failed,flush=True)
            print(ops.tmux(state.owners.config,'capture-pane','-p','-J','-S','-80',
                '-t','='+owner.spec.spec['tmux_name']+':').stdout[-6000:],flush=True)
            raise
        identifier,pending=next(iter(state.owners.hooks.pending.items()))
        start=time.monotonic()
        assert not hasattr(owner,'pending')  # actual TUI, not the SDK callback
        async with asyncio.timeout(3):
            while True:
                with c.db() as db:
                    row=db.get(RelayDecision,(sid,identifier))
                    if row:
                        card=dict(row.decision)
                        break
                await asyncio.sleep(.01)
        remaining=(card['expiresAtMs']-time.time_ns()//1_000_000)/1000
        assert 13<=remaining<=15
        async with asyncio.timeout(30):
            while (identifier in state.owners.hooks.pending or owner.host.processed!=4
                    or launch.terminal_grants.record.processed!=4 or self.base.native_denied_at is None):
                await asyncio.sleep(.05)
        delay=self.base.native_denied_at-start
        assert remaining-.15<=delay<=20,delay
        with c.db() as db:
            final=dict(db.get(RelayDecision,(sid,identifier)).decision)
            assert final['status']=='expired' and final.get('winner') is None
        assert not (self.base.work/'tui-expiry-target').exists()
        for label,resolve in [('chat',c.resolve),('attached_terminal',attached)]:
            fields={'sessionId':sid,'expectedRunId':run,'decisionId':identifier,'choiceId':'accept',
                'expectedRevision':card['revision'],'actionDigest':card['actionDigest']}
            registered=Hub(c.ctx).register(resolve,sid,run,'late-tui-'+label,'decision.reply',p.operation_digest('decision.reply',fields))
            for attempt in range(2):
                try:await launch.answer(resolve,{**fields,'operationId':registered.operation_id})
                except p.RelayError as error:assert error.code is (p.ErrorCode.DECISION_EXPIRED if attempt==0 else p.ErrorCode.DECISION_RESOLVED)
                else:raise AssertionError('Expired TUI permission acquired a winner')
            with c.db() as db:
                receipt=db.get(RelayOperation,registered.operation_id).receipt
                assert receipt['state']=='failed' and receipt['reason']=='expired'
        await frames(5,'Safe native continuation after unanswered expiry')
        async with asyncio.timeout(20):
            while owner.host.processed!=6 or launch.terminal_grants.record.processed!=6:await asyncio.sleep(.05)
        assert owner.process.current() and not state.owners.hooks.pending
        proof.update(nativeTuiPermissionHook=True,nativeQuestionExpired=True,decisionWinnerAbsent=True,
            nativeDeadlineSeconds=15,remainingAtCaptureSeconds=remaining,nativeDenialSeconds=delay,
            nativeToolId=pending['nativeToolId'],clockPatched=False,deadlineShortened=False,
            lateChoicesRefusedWithoutDispatch=True,noReplay=True,toolNeverExecuted=True,
            followupNativeInputProcessed=True,modelCalls=self.base.calls,
            scope='Actual granted production native TUI unanswered question and final-main expiry/continuation. Late attached reply uses internal resolver, not stock native approval keys; first-valid public TUI/Chat race and conclusive chosen-answer reconciliation remain open.')


async def prepare(root,work,c):
    frozen=await prepare_frozen(root,work,c)
    native=frozen.base.base
    original=native.claude_events
    native.native_tool_offered=False
    native.native_denied_at=None
    def events(payload):
        for message in payload.get('messages',[]):
            for item in message.get('content',[]) if isinstance(message.get('content'),list) else []:
                if (item.get('type')=='tool_result' and item.get('tool_use_id')=='installed_security_tool'
                        and item.get('is_error') is True and 'denied' in json.dumps(item.get('content','')).lower()):
                    if native.native_denied_at is None:native.native_denied_at=time.monotonic()
        if (not native.native_tool_offered and QUESTION in json.dumps(payload.get('messages',[]))
                and any(tool.get('name')=='Bash' for tool in payload.get('tools',[]))):
            native.native_tool_offered=True
            command='python3 -c '+shlex.quote('from pathlib import Path; Path("tui-expiry-target").write_text("forbidden")')
            return Security.claude_events(SimpleNamespace(calls=1,command=command),payload)
        return original(payload)
    native.claude_events=events
    return Case(frozen.base)
