"""Native rejection metadata and exact invocation reference are both required."""
import copy

import pytest
from test_session_relay_claude_admission import append, create
from test_session_relay_hook_races import hook_case  # noqa: F401

from acc.broker.claude_denial_evidence import NativeDenial
from acc.session_relay import protocol as p
from acc.session_relay.claude_hook import denial_message

REF = p.sha256_digest(b'private invocation')


def records():
    common = {'sessionId':'native-fixture','isSidechain':False,'parentUuid':'assistant-tool'}
    return [{**common,'type':'attachment','uuid':'decision-1','attachment':
                {'type':'hook_permission_decision','hookEvent':'PermissionRequest','decision':'deny','toolUseID':'tool-1'}},
            {**common,'type':'user','uuid':'result-1','promptId':'query-1','sourceToolAssistantUUID':'assistant-tool',
                'permissionDecision':{'decision':'reject','reasonCode':'hook:PermissionRequest','reasonType':'hook','source':'hook'},
                'toolDenialKind':'permission-rule','toolUseResult':'Error: '+denial_message(REF),
                'message':{'role':'user','content':[{'type':'tool_result','tool_use_id':'tool-1',
                    'is_error':True,'content':denial_message(REF)}]}}]


def reader(tmp_path):
    path, old = create(tmp_path)
    old.close()
    append(path,[{'uuid':'assistant-tool','parentUuid':None,'type':'assistant','sessionId':'native-fixture',
        'isSidechain':False,'message':{'role':'assistant','id':'api-message','content':[{'type':'tool_use','id':'tool-1','name':'Bash','input':{}}]}}])
    return path, NativeDenial(path.parent.parent,path,'native-fixture')


def test_exact_chosen_denial_only_once(tmp_path):
    path, proof = reader(tmp_path)
    try:
        proof.begin()
        append(path,records())
        assert p.valid_digest(proof.denied(REF,'query-1'))
        assert proof.denied(REF,'query-1') is None
    finally:
        proof.close()


@pytest.mark.parametrize('fault',['history','missing_attachment','allow','other_tool','other_parent',
    'other_reference','other_prompt','plain_text','successful_tool','non_hook','wrong_reason','no_denial_kind',
    'duplicate_result','duplicate_decision','wrong_session','sidechain','rewritten_prefix','replace','symlink'])
def test_unbound_or_untrusted_denial_does_not_ack(tmp_path,fault):
    path, proof = reader(tmp_path)
    try:
        rows = records()
        if fault == 'history':append(path,rows)
        if fault == 'rewritten_prefix':append(path,[{'marker':'before'}])
        proof.begin()
        if fault == 'missing_attachment':rows = rows[1:]
        elif fault == 'allow':rows[0]['attachment']['decision'] = 'allow'
        elif fault == 'other_tool':rows[0]['attachment']['toolUseID'] = 'tool-2'
        elif fault == 'other_parent':rows[0]['parentUuid'] = 'other-assistant'
        elif fault == 'other_reference':rows[1]['message']['content'][0]['content'] = denial_message(p.sha256_digest(b'other'))
        elif fault == 'other_prompt':rows[1]['promptId'] = 'other-query'
        elif fault == 'plain_text':rows[1]['type'] = 'assistant'
        elif fault == 'successful_tool':rows[1]['message']['content'][0]['is_error'] = False
        elif fault == 'non_hook':rows[1]['permissionDecision']['source'] = 'config'
        elif fault == 'wrong_reason':rows[1]['permissionDecision']['reasonCode'] = 'other'
        elif fault == 'no_denial_kind':rows[1].pop('toolDenialKind')
        elif fault == 'duplicate_result':rows.append({**copy.deepcopy(rows[1]),'uuid':'result-2'})
        elif fault == 'duplicate_decision':rows.append({**copy.deepcopy(rows[0]),'uuid':'decision-2'})
        elif fault == 'wrong_session':rows[1]['sessionId'] = 'other-session'
        elif fault == 'sidechain':rows[1]['isSidechain'] = True
        if fault != 'history':append(path,rows)
        if fault == 'rewritten_prefix':path.write_text(path.read_text().replace('before','broken'))
        if fault == 'replace':
            path.unlink();append(path,rows)
        if fault == 'symlink':
            other = path.with_suffix('.other');path.rename(other);path.symlink_to(other)
        try:
            assert proof.denied(REF,'query-1') is None
        except (p.RelayError,OSError):
            assert fault in ('wrong_session','sidechain','rewritten_prefix','replace','symlink')
    finally:
        proof.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault',['none','wrong_reference','missing_native','replaced','lost_publication_ack'])
async def test_callback_write_is_never_the_application_proof(tmp_path,fault):
    from types import SimpleNamespace

    from acc.broker.claude_denial_evidence import answer, close
    path, old = reader(tmp_path)
    old.close()
    publications=[]
    class Hooks:
        pending={'decision-1':{'promptId':'query-1'}}
        async def answer(self,identifier,digest,effect,verify,*,deny_reference=None):
            assert await verify() and effect=='deny' and p.valid_digest(deny_reference)
            if fault=='missing_native':return {'outcome':'unknown'}
            rows=records()
            ref=REF if fault=='wrong_reference' else deny_reference
            rows[1]['message']['content'][0]['content']=denial_message(ref)
            rows[1]['toolUseResult']='Error: '+denial_message(ref)
            if fault=='replaced':path.unlink()
            append(path,rows)
            return {'outcome':'unknown'}
    async def publish(event):
        publications.append(event)
        if fault=='lost_publication_ack' and len(publications)==1:raise ConnectionError('lost after durable original settlement')
        return True
    async def verify():return True
    runtime=SimpleNamespace(owners=SimpleNamespace(hooks=Hooks(),environment=lambda spec:{
        'HOME':str(tmp_path),'CLAUDE_CONFIG_DIR':str(tmp_path)}),
        native_transcript=('run-1',str(path)),spec=SimpleNamespace(run_id='run-1',
            binding=SimpleNamespace(native_conversation='native-fixture')),
        guard_check=lambda:None,publish=publish,peer=SimpleNamespace(closed=False))
    value={'decisionId':'decision-1','actionDigest':p.sha256_digest(b'action'),'effect':'deny',
        'operation':{'operationId':'original-operation'}}
    try:
        result=await answer(runtime,value,verify)
        assert result['outcome']==('accepted' if fault in ('none','lost_publication_ack') else 'unknown')
        assert len(publications)==({'none':1,'lost_publication_ack':2}.get(fault,0))
        if publications:
            assert publications[0]['operationId']=='original-operation' and publications[0]['choiceId']=='decline'
            assert all(event==publications[0] for event in publications)
    finally:
        await close(runtime)
    assert not runtime.denial_observers



@pytest.mark.asyncio
@pytest.mark.parametrize('effect,reference',[('deny',REF),('allow_once',REF),('deny','untrusted')])
async def test_reference_is_consumed_only_by_exact_valid_deny(hook_case,effect,reference):  # noqa: F811
    import asyncio
    server,card,task=hook_case
    async def verify():return True
    if effect=='deny' and reference==REF:
        assert await server.answer(card['decisionId'],card['actionDigest'],effect,verify,
            deny_reference=reference)=={'outcome':'unknown'}
        assert await asyncio.wait_for(task,5)=={'behavior':'deny','denyReference':REF}
    else:
        with pytest.raises(p.RelayError):
            await server.answer(card['decisionId'],card['actionDigest'],effect,verify,deny_reference=reference)
        pending=server.pending[card['decisionId']]
        assert not pending['claimed'] and not pending['future'].done()


@pytest.mark.asyncio
async def test_late_evidence_retains_original_run_after_foreground_unknown(tmp_path,monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from acc.broker.claude_denial_evidence import answer, close
    path,old=reader(tmp_path);old.close()
    visible=False;writes=[];publications=[]
    native=NativeDenial.denied
    def delayed(self,reference,prompt):return native(self,reference,prompt) if visible else None
    monkeypatch.setattr(NativeDenial,'denied',delayed)
    class Hooks:
        pending={'decision-1':{'promptId':'query-1'}}
        async def answer(self,*args,deny_reference=None):
            writes.append(deny_reference)
            rows=records();rows[1]['message']['content'][0]['content']=denial_message(deny_reference)
            rows[1]['toolUseResult']='Error: '+denial_message(deny_reference);append(path,rows)
            return {'outcome':'unknown'}
    async def peer_call(method,params):
        assert method=='publication';publications.append(params);return True
    async def wrong_run(event):raise AssertionError('Late evidence routed as replacement run')
    runtime=SimpleNamespace(owners=SimpleNamespace(hooks=Hooks(),environment=lambda spec:{'HOME':str(tmp_path),'CLAUDE_CONFIG_DIR':str(tmp_path)}),
        native_transcript=('run-1',str(path)),spec=SimpleNamespace(run_id='run-1',binding=SimpleNamespace(native_conversation='native-fixture')),
        guard_check=lambda:None,publish=wrong_run,peer=SimpleNamespace(closed=False,call=peer_call))
    async def verify():return True
    value={'decisionId':'decision-1','actionDigest':p.sha256_digest(b'action'),'effect':'deny','operation':{'operationId':'original'}}
    try:
        assert await answer(runtime,value,verify)=={'outcome':'unknown'}
        assert len(runtime.denial_observers)==1 and not publications
        runtime.spec.run_id='run-2';visible=True
        async with asyncio.timeout(2):
            while runtime.denial_observers:await asyncio.sleep(.01)
        assert len(writes)==1 and len(publications)==1
        assert publications[0]['runId']=='run-1' and publications[0]['operationId']=='original'
    finally:await close(runtime)


@pytest.mark.asyncio
async def test_cancelled_foreground_preserves_observer_until_runtime_close(tmp_path,monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from acc.broker.claude_denial_evidence import answer, close
    path,old=reader(tmp_path);old.close()
    readers=[];begin=NativeDenial.begin
    def capture(self):readers.append(self);begin(self)
    monkeypatch.setattr(NativeDenial,'begin',capture)
    class Hooks:
        pending={'decision-1':{'promptId':'query-1'}}
        async def answer(self,*args,**kwargs):return {'outcome':'unknown'}
    runtime=SimpleNamespace(owners=SimpleNamespace(hooks=Hooks(),environment=lambda spec:{'HOME':str(tmp_path),'CLAUDE_CONFIG_DIR':str(tmp_path)}),
        native_transcript=('run-1',str(path)),spec=SimpleNamespace(run_id='run-1',binding=SimpleNamespace(native_conversation='native-fixture')),
        guard_check=lambda:None)
    async def verify():return True
    task=asyncio.create_task(answer(runtime,{'decisionId':'decision-1','actionDigest':REF,'effect':'deny','operation':{'operationId':'original'}},verify))
    async with asyncio.timeout(2):
        while not getattr(runtime,'denial_observers',None):await asyncio.sleep(.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert len(runtime.denial_observers)==1 and readers[0].fd is not None
    await close(runtime)
    assert not runtime.denial_observers and readers[0].fd is None
    await close(runtime)


def test_parallel_native_attachment_parent_can_follow_the_other_tool_branch(tmp_path):
    path,proof=reader(tmp_path)
    other={'uuid':'assistant-twin','parentUuid':'assistant-tool','type':'assistant','sessionId':'native-fixture',
        'isSidechain':False,'message':{'role':'assistant','id':'api-message-twin',
            'content':[{'type':'tool_use','id':'tool-twin','name':'Read','input':{}}]}}
    append(path,[other])
    reference=p.sha256_digest(b'twin-private-reference')
    rows=records()
    twin=copy.deepcopy(rows)
    twin[0].update(uuid='decision-twin',parentUuid=rows[1]['uuid'])
    twin[0]['attachment']['toolUseID']='tool-twin'
    twin[1].update(uuid='result-twin',parentUuid='assistant-twin',sourceToolAssistantUUID='assistant-twin')
    twin[1]['message']['content'][0].update(tool_use_id='tool-twin',content=denial_message(reference))
    twin[1]['toolUseResult']='Error: '+denial_message(reference)
    try:
        proof.tool_name='Read';proof.begin();append(path,rows+twin)
        assert p.valid_digest(proof.denied(reference,'query-1'))
    finally:proof.close()


@pytest.mark.parametrize('fault',['missing_tool_source','wrong_tool_source','wrong_tool_name'])
def test_exact_native_tool_source_is_required(tmp_path,fault):
    path,proof=reader(tmp_path)
    try:
        prefix=path.read_text()
        if fault=='missing_tool_source':prefix=prefix.replace('"assistant"','"user"')
        elif fault=='wrong_tool_source':prefix=prefix.replace('"tool-1"','"unrelated-tool"')
        elif fault=='wrong_tool_name':proof.tool_name='Read'
        path.write_text(prefix);proof.begin();append(path,records())
        assert proof.denied(REF,'query-1') is None
    finally:proof.close()


@pytest.mark.asyncio
async def test_observer_capacity_refuses_allocation_before_a_referenced_response():
    from types import SimpleNamespace

    from acc.broker.claude_denial_evidence import answer
    answers=[]
    class Hooks:
        pending={'decision-1':{'promptId':'query-1'}}
        async def answer(self,*args,**kwargs):answers.append(kwargs);return {'outcome':'unknown'}
    def unavailable(spec):raise AssertionError('Capacity refusal must not allocate/read an evidence source')
    runtime=SimpleNamespace(owners=SimpleNamespace(hooks=Hooks(),environment=unavailable),
        native_transcript=('run-1','unused'),spec=SimpleNamespace(run_id='run-1'),denial_observers=set(range(16)))
    async def verify():return True
    assert await answer(runtime,{'decisionId':'decision-1','actionDigest':REF,'effect':'deny'},verify)=={'outcome':'unknown'}
    assert answers==[{}] and len(runtime.denial_observers)==16
