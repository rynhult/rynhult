"""Actual native retained-draft replacement through authenticated public plans."""
from enrolled_retained_draft import Case as RetainedCase
from enrolled_retained_draft import prepare as prepare_retained
from public_native_http import Public

from acc.server.db import write_intent
from acc.server.models import RelayOperation, RelayOwnership, RelaySession
from acc.server.session_relay import exclusive_public
from acc.session_relay import protocol as p


class Case(RetainedCase):
    async def exercise(self,c,state,launch,manager,config):
        exclusive_public.PUBLIC_L1_QUALIFIED=True;c.ctx.relay_runtime=manager
        original=manager.select_owner;sid=state.spec.spec['session_id'];operations=[];cancelled=[]
        public=await Public.create(c,manager,sid,features=[])
        @write_intent
        def qualify():
            with c.db() as db:
                row=db.get(RelaySession,sid);before=dict(row.capabilities)
                row.capabilities={**before,'switch.to_chat':'qualified','switch.to_terminal':'qualified'}
                db.commit();return before,str(row.current_run_id)
        @write_intent
        def restore(before,run):
            with c.db() as db:
                row=db.get(RelaySession,sid)
                if str(row.current_run_id)==run:row.capabilities=before;db.commit()
        async def negotiate():
            value=(await public.call('initialize',{'major':1,'minor':0,'features':['switch'],'clientId':'offline-public-l1'}))['result']
            public.client.headers['x-acc-relay-connection']=value['connectionToken']
            await public.call('session.attach',{'clientId':'offline-public-l1','connectionSeq':1})
        async def mutate(method,fields,key):
            registration=await public.call('operation.register',{'expectedRunId':fields['expectedRunId'],
                'registrationKey':key,'method':method,'payloadDigest':p.operation_digest(method,fields)})
            assert 'result' in registration,registration
            operation=registration['result']['operationId'];operations.append(operation)
            result=await public.call(method,{**fields,'operationId':operation})
            if 'error' in result:raise p.RelayError(p.ErrorCode(result['error']['data']['code']))
            return result['result'],operation
        async def select(resolve,session_id,*,target,revision,request_key):
            assert session_id==sid
            resolve()  # preserve the base fixture's explicit revoked-origin negative case
            before,run=qualify()
            try:
                await negotiate()
                snapshot=(await public.call('session.snapshot',{}))['result']
                fields={'sessionId':sid,'expectedRunId':run,'target':target,'expectedRevision':snapshot['l1']['revision']}
                plan,prepare_id=await mutate('switch.prepare',fields,request_key+'-prepare')
                if not cancelled:
                    value,_=await mutate('switch.cancel',{'sessionId':sid,'expectedRunId':run,'planId':plan['planId']},'cancel-before-claim')
                    assert value['phase']=='owned';cancelled.append(True)
                    fields['expectedRevision']=value['revision']
                    plan,prepare_id=await mutate('switch.prepare',fields,request_key+'-prepare-after-cancel')
                result,commit_id=await mutate('switch.commit',{'sessionId':sid,'expectedRunId':run,
                    'planId':plan['planId'],'planDigest':plan['planDigest'],'expectedRevision':plan['revision']},request_key+'-commit')
                assert result['prepareOperationId']==prepare_id and result['operationId']==commit_id
                with c.db() as db:
                    assert db.get(RelayOperation,prepare_id).receipt['state']=='accepted'
                    assert db.get(RelayOperation,commit_id).receipt['state']=='accepted'
                    assert db.get(RelayOwnership,sid).phase=='owned'
                stale=await public.call('session.snapshot',{})
                assert stale['error']['data']['code']=='NOT_FOUND_OR_DENIED'
                assert await state.owners.authorize()  # continuing person lifecycle, no obsolete input scope
                return result
            finally:restore(before,run)
        manager.select_owner=select
        try:
            await super().exercise(c,state,launch,manager,config)
            await negotiate()
            value=(await public.call('session.snapshot',{}))['result']
            assert value['retainedInputs'] and value['retainedInputs'][0]['readOnly'] is True
            import json
            from pathlib import Path
            path=Path('/evidence/result.json');proof=json.loads(path.read_text())
            proof.update(publicL1CookiePlans=True,publicL1OriginalReceipts=True,publicL1PreclaimCancel=True,
                oldConnectionNeverRenewed=True,newOwnerFreshPersonLifecycle=True,publicRetainedDraftVisible=True,
                publicOperationCount=len(operations),scope='Actual enrolled offline API-key native SDK/TUI/SDK/TUI same-id retained draft/fresh input, real public cookie/CSRF prepare/commit/cancel and original durable receipts. Old connection never renewed; new owner lifecycle uses fresh person auth. Text-frame fixture principal/capability overrides remain explicit; feature and terminal mediation stay off outside this isolated lab.')
            path.write_text(json.dumps(proof))
        finally:manager.select_owner=original;await public.close()


async def prepare(root,work,c):
    retained=await prepare_retained(root,work,c)
    return Case(retained.base)
