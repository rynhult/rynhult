"""Real enrolled Claude permission; production Hub CAS, no invented native ACK."""
import asyncio
import json
from datetime import timedelta
from pathlib import Path

from installed_security import prepare as prepare_security
from sqlalchemy import select

from acc.server.db import write_intent
from acc.server.models import RelayDecision, RelayOperation, utcnow
from acc.server.session_relay.hub import Hub
from acc.session_relay import protocol as p


class Case:
    def __init__(self, base):
        self.base = base

    def __getattr__(self, name):
        return getattr(self.base, name)

    async def exercise(self, c, state, launch, manager, config):
        # This probe proves the no-application-evidence boundary. A qualified
        # primary reader can now establish application, so withhold that separate
        # evidence explicitly for either CAS winner; native writes/IDs stay real.
        applications=state.owners.current.applications
        original=applications.observe
        applications.observe=lambda *_:None
        try:
            await self.unknown_exercise(c,state,launch,manager,config)
            path=Path('/evidence/result.json');proof=json.loads(path.read_text())
            proof['primaryApplicationEvidenceWithheld']=True
            path.write_text(json.dumps(proof))
        finally:
            applications.observe=original

    async def unknown_exercise(self, c, state, launch, manager, config):
        sid, run = state.spec.spec['session_id'], str(launch.run_id)
        hub = Hub(c.ctx)
        params = {'sessionId':sid,'expectedRunId':run,'text':'Offline exact decision fixture',
            'expectedConfigurationRevision':0,'expectedControlRevision':0,'ifBusy':'reject'}
        op = hub.register(c.resolve,sid,run,'decision-turn','turn.submit',p.operation_digest('turn.submit',params))
        await manager.submit(c.resolve,sid,{**params,'operationId':op.operation_id})
        def pending():
            return state.owners.hooks.pending or state.adapter.pending
        async with asyncio.timeout(10):
            while not pending():
                await asyncio.sleep(.01)
        identifier = next(iter(pending()))
        async with asyncio.timeout(3):
            while True:
                with c.db() as db:
                    record = db.get(RelayDecision,(sid,identifier))
                    if record:
                        decision = dict(record.decision)
                        break
                await asyncio.sleep(.01)
        native_hook = identifier in state.owners.hooks.pending
        answer = {'sessionId':sid,'expectedRunId':run,'decisionId':identifier,
            'choiceId':'decline','expectedRevision':decision['revision'],'actionDigest':decision['actionDigest']}
        invalid = []
        for name, changes in [('choice',{'choiceId':'allow_always'}),
                              ('digest',{'actionDigest':p.sha256_digest(b'wrong')}),
                              ('revision',{'expectedRevision':decision['revision']+1}),('expired_operation',{})]:
            fields = {**answer,**changes}
            operation = hub.register(c.resolve,sid,run,'invalid-'+name,'decision.reply',p.operation_digest('decision.reply',fields))
            if name == 'expired_operation':
                @write_intent
                def expire():
                    with c.db() as db:
                        db.get(RelayOperation,operation.operation_id).deadline = utcnow()-timedelta(seconds=1)
                        db.commit()
                expire()
            try:
                await launch.answer(c.resolve,{**fields,'operationId':operation.operation_id})
            except p.RelayError:
                invalid.append(name)
            else:
                raise AssertionError('Invalid native decision consumed: '+name)
        terminal_actor = p.Actor(p.ActorKind.PERSON,'alex',p.Channel.TERMINAL,'offline-terminal')
        revoked = False
        def terminal():
            who, _ = c.resolve()
            if revoked:
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            return who, terminal_actor
        operation = hub.register(terminal,sid,run,'revoked','decision.reply',p.operation_digest('decision.reply',answer))
        revoked = True
        try:
            await launch.answer(terminal,{**answer,'operationId':operation.operation_id})
        except p.RelayError:
            invalid.append('revoked')
        else:
            raise AssertionError('Revoked decision consumed')
        revoked = False
        contenders = []
        for name,resolve,choice in [('terminal',terminal,'accept'),('chat',c.resolve,'decline')]:
            fields = {**answer,'choiceId':choice}
            operation = hub.register(resolve,sid,run,'race-'+name,'decision.reply',p.operation_digest('decision.reply',fields))
            contenders.append((name,resolve,{**fields,'operationId':operation.operation_id}))
        start = asyncio.Event()
        async def compete(contender):
            name,resolve,fields = contender
            await start.wait()
            try:
                result = await launch.answer(resolve,fields)
            except p.RelayError as error:
                return {'actor':name,'error':error.code.value}
            return {'actor':name,'result':result}
        tasks = [asyncio.create_task(compete(item)) for item in contenders]
        start.set()
        outcomes = await asyncio.gather(*tasks)
        winners = [item for item in outcomes if 'result' in item]
        assert len(winners)==1 and winners[0]['result']=={'outcome':'unknown'}, outcomes
        async with asyncio.timeout(10):
            while self.base.calls < 2 or state.adapter.active:
                await asyncio.sleep(.02)
        with c.db() as db:
            final = db.get(RelayDecision,(sid,identifier)).decision
            winner = next(item for item in contenders if item[0]==winners[0]['actor'])
            receipt = db.get(RelayOperation,winner[2]['operationId']).receipt
            assert final['status']=='outcome_unknown' and receipt['state']=='uncertain'
            assert final['winner']['channel'] == ('terminal' if winner[0]=='terminal' else 'chat')
        try:
            await launch.answer(winner[1],winner[2])
        except p.RelayError:
            pass
        else:
            raise AssertionError('Unknown native answer replayed')
        assert self.base.calls == 2
        with c.db() as db:
            assert [row.id for row in db.scalars(select(RelayDecision).where(RelayDecision.session_id==sid))]==[identifier]
        assert native_hook is False and not state.owners.hooks.pending
        Path('/evidence/result.json').write_text(json.dumps({'provider':'claude',
            'nativeId':state.binding.native_conversation,'nativePermissionHook':native_hook,
            'realNativeOwner':True,'productionHubCAS':True,'invalidRefused':invalid,'oneDecisionAuthority':True,
            'contenders':outcomes,'winner':winners[0]['actor'],'unknownPreserved':True,
            'unknownNotReplayed':True,'modelCalls':2,'terminalMediated':False,
            'declaredCapabilities':{},'scope':'Real CLI permission plus Hub Terminal/Chat resolver race; terminal resolver is artificial, not a public native TUI input grant. Conclusive native reconciliation remains unavailable.'}))


async def prepare(root, work, c):
    base = await prepare_security(root,work,c)
    base.command = 'printf fixture > /tmp/decision-target'
    return Case(base)
