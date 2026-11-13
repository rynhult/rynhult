"""Real five-minute SDK permission expiry; no clock patch or shortened timeout."""
import asyncio
import json
import time
from pathlib import Path

from installed_security import prepare as prepare_security
from sqlalchemy import select

from acc.server.models import RelayDecision, RelayOperation
from acc.server.session_relay.hub import Hub
from acc.session_relay import protocol as p


class Case:
    def __init__(self,base):
        self.base=base

    def __getattr__(self,name):
        return getattr(self.base,name)

    async def exercise(self,c,state,launch,manager,config):
        sid,run=state.spec.spec['session_id'],str(launch.run_id)
        hub=Hub(c.ctx)
        params={'sessionId':sid,'expectedRunId':run,'text':'Offline natural permission expiry',
            'expectedConfigurationRevision':0,'expectedControlRevision':0,'ifBusy':'reject'}
        op=hub.register(c.resolve,sid,run,'natural-expiry-turn','turn.submit',p.operation_digest('turn.submit',params))
        await manager.submit(c.resolve,sid,{**params,'operationId':op.operation_id})
        async with asyncio.timeout(15):
            while not state.adapter.pending:
                await asyncio.sleep(.01)
        identifier=next(iter(state.adapter.pending))
        assert not state.owners.hooks.pending  # This proof is the real SDK callback.
        async with asyncio.timeout(3):
            while True:
                with c.db() as db:
                    row=db.get(RelayDecision,(sid,identifier))
                    if row:
                        decision=dict(row.decision)
                        break
                await asyncio.sleep(.01)
        deadline=decision['expiresAtMs']
        remaining=(deadline-time.time_ns()//1_000_000)/1000
        assert 295<=remaining<=300
        started=time.monotonic()
        # Keep production heartbeat/leader and SDK event loops running. The
        # actual native five-minute permission deadline is never patched.
        async with asyncio.timeout(320):
            while identifier in state.adapter.pending:
                await asyncio.sleep(.5)
        elapsed=time.monotonic()-started
        assert remaining-.1<=elapsed<=315
        async with asyncio.timeout(12):
            while self.base.calls<2 or state.adapter.active:
                await asyncio.sleep(.05)
        assert self.base.calls==2 and self.base.native_denial, self.base.native_results
        denial_delay=self.base.denial_at-started
        assert 295<=denial_delay<=315, {'nativeDenialDelaySeconds':denial_delay,'nativeResults':self.base.native_results}
        assert not Path('/tmp/natural-expiry-target').exists()
        with c.db() as db:
            final=dict(db.get(RelayDecision,(sid,identifier)).decision)
            assert final['status']=='expired' and final.get('winner') is None
            cards=list(db.scalars(select(RelayDecision).where(RelayDecision.session_id==sid)))
            assert [card.id for card in cards]==[identifier]
        assert not state.owners.hooks.pending
        late=[]
        for channel in (p.Channel.CHAT,p.Channel.TERMINAL):
            actor=p.Actor(p.ActorKind.PERSON,'alex',channel,'offline-expiry' if channel is p.Channel.TERMINAL else None)
            def resolve(actor=actor):
                return c.resolve()[0],actor
            answer={'sessionId':sid,'expectedRunId':run,'decisionId':identifier,'choiceId':'accept',
                'expectedRevision':decision['revision'],'actionDigest':decision['actionDigest']}
            # Register AFTER expiry so operation TTL cannot be confused with
            # expiration of the native question itself.
            registered=hub.register(resolve,sid,run,'late-'+channel.value,'decision.reply',p.operation_digest('decision.reply',answer))
            for attempt in range(2):
                try:
                    await launch.answer(resolve,{**answer,'operationId':registered.operation_id})
                except p.RelayError as error:
                    assert error.code is (p.ErrorCode.DECISION_EXPIRED if attempt==0 else p.ErrorCode.DECISION_RESOLVED)
                else:
                    raise AssertionError('An expired native question acquired a winner')
            with c.db() as db:
                receipt=db.get(RelayOperation,registered.operation_id).receipt
                assert receipt['state']=='failed' and receipt['reason']=='expired'  # Never dispatched; cannot block follow-up.
            late.append(channel.value)
        with c.db() as db:
            assert db.get(RelayDecision,(sid,identifier)).decision==final
        assert self.base.calls==2 and not state.adapter.pending and not state.adapter.pending_meta
        Path('/evidence/result.json').write_text(json.dumps({'provider':'claude',
            'nativeId':state.binding.native_conversation,'nativeSdkCallback':True,
            'publishedDeadlineMs':deadline,'remainingAtCaptureSeconds':remaining,'actualWaitSeconds':elapsed,
            'nativeDeadlineSeconds':300,'clockPatched':False,'deadlineShortened':False,
            'nativeConsumedToolDenialAtExpiry':True,'nativeCustomExpiryMessageObserved':self.base.expiry_denial_seen,
            'nativeDenialDelaySeconds':denial_delay,'durableDecisionExpired':True,'winnerAbsent':True,
            'oneDecisionAuthority':True,'nativeHookDecisionAbsent':True,
            'lateAnswerChannelsRefused':late,'lateOperationsNeverClaimed':True,'repeatNeverReplayed':True,
            'metadataReadForAdmission':False,'modelCalls':2,'toolNeverExecuted':True,
            'terminalMediated':False,'declaredCapabilities':{},
            'scope':'Real native SDK permission absolute expiry and main Hub refusal. Terminal resolver is artificial; no public native TUI decision transport or conclusive chosen-answer settlement claimed.'}))


async def prepare(root,work,c):
    base=await prepare_security(root,work,c)
    base.command='printf forbidden > /tmp/natural-expiry-target'
    base.expiry_denial_seen=False
    base.native_denial=False
    base.native_results=[]
    base.denial_at=None
    original=base.claude_events
    def observe(payload):
        base.expiry_denial_seen |= 'This native decision expired.' in json.dumps(payload)
        for message in payload.get('messages',[]):
            for item in message.get('content',[]) if isinstance(message.get('content'),list) else []:
                if item.get('type')=='tool_result' and item.get('tool_use_id')=='installed_security_tool':
                    text=json.dumps(item.get('content','')).lower()
                    denied=item.get('is_error') is True and any(marker in text for marker in
                        ('denied','expired','timed out','timeout',"doesn't want to proceed"))
                    base.native_results.append(item)
                    if denied and not base.native_denial:
                        base.native_denial=True
                        base.denial_at=time.monotonic()
        return original(payload)
    base.claude_events=observe
    return Case(base)
