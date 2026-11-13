"""Original private evidence survives process lifetime; never resends an answer."""
import copy

import pytest
from test_session_relay_claude_allow_evidence import reader

from acc.broker.claude_decision_journal import DecisionEvidenceJournal, checkpoint, reconcile, restore
from acc.broker.claude_tool_binding import Completion
from acc.session_relay import protocol as p


def prepared(tmp_path):
    path,value,binding=reader(tmp_path)
    snap=checkpoint(value);value.close()
    scope=vars(p.Binding('binding','installation','node','account',str(tmp_path),'claude','native-fixture',p.sha256_digest(b'policy')))
    record={'event':{'kind':'decision.applied','decisionId':'decision','operationId':'operation','choiceId':'accept','actionDigest':p.sha256_digest(b'action')},
        'runId':'1','reader':snap,'binding':binding,'operation':{'operationId':'operation','runId':'1','method':'decision.reply',
            'actor':{'kind':'person','principal':'alex','channel':'chat'},'payloadDigest':p.sha256_digest(b'payload')}}
    journal=DecisionEvidenceJournal(tmp_path/'journal');journal.prepare(scope,record)
    completion=Completion('native-fixture','tool-1',binding['promptDigest'],'PostToolUse',p.sha256_digest(b'native'))
    return path,scope,record,journal,completion


async def test_reopen_after_lost_publication_ack_preserves_original_receipt_and_no_replay(tmp_path):
    path,scope,record,journal,event=prepared(tmp_path)
    journal.sent(scope,event.tool,event.digest);journal.complete(scope,event)
    events=[]
    async def lost(value):events.append(value);raise ConnectionError('lost acknowledgement')
    with pytest.raises(ConnectionError):await reconcile(journal,scope,path.parent.parent,lost)
    assert journal.pending(scope)[0][3]
    journal.close();journal=DecisionEvidenceJournal(tmp_path/'journal')
    async def accept(value):assert value==events[0];events.append(value);return True
    try:
        assert await reconcile(journal,scope,path.parent.parent,accept)==1
        assert await reconcile(journal,scope,path.parent.parent,accept)==0
        assert len(events)==2 and events[0]['originalOperation']==record['operation']
        assert not journal.pending(scope)
    finally:journal.close()


@pytest.mark.parametrize('sent',[False,True])
async def test_prepared_or_delivered_without_native_evidence_never_becomes_application(tmp_path,sent):
    path,scope,record,journal,event=prepared(tmp_path)
    try:
        if sent:journal.sent(scope,event.tool,event.digest)
        else:journal.complete(scope,event)  # a prerequisite completion cannot fill an unsent Allow
        calls=[]
        async def publish(value):calls.append(value);return True
        assert await reconcile(journal,scope,path.parent.parent,publish)==0
        assert not calls and len(journal.pending(scope,prepared=True))==1
    finally:journal.close()


@pytest.mark.parametrize('fault',['root','project','file','prefix','offset','symlink','truncated','foreign_scope','foreign_tool','foreign_digest','interrupted'])
async def test_changed_source_or_foreign_completion_cannot_settle(tmp_path,fault):
    path,scope,record,journal,event=prepared(tmp_path)
    journal.sent(scope,event.tool,event.digest)
    try:
        if fault=='foreign_scope':scope={**scope,'account':'foreign'}
        elif fault=='foreign_tool':event=Completion(event.session,'foreign',event.digest,event.event,event.reference)
        elif fault=='foreign_digest':event=Completion(event.session,event.tool,p.sha256_digest(b'foreign'),event.event,event.reference)
        if fault=='interrupted':journal.observe(scope,{'hook_event_name':'PostToolUse','session_id':event.session,'tool_use_id':event.tool,
            'prompt_id':'query-1','tool_name':'Bash','tool_input':{'command':'fixture'},'is_interrupt':True})
        else:journal.complete(scope,event)
        if fault in ('root','project','file','prefix','offset'):
            snapshot=copy.deepcopy(record['reader'])
            if fault=='prefix':snapshot['prefix']='00'*32
            elif fault=='offset':snapshot['offset']+=1
            else:snapshot[{'root':'root','project':'projectIdentity','file':'file'}[fault]][1]+=1
            with pytest.raises(p.RelayError):restore(path.parent.parent,snapshot)
            return
        if fault=='symlink':
            other=path.with_suffix('.other');path.rename(other);path.symlink_to(other)
        elif fault=='truncated':path.write_bytes(b'')
        async def publish(value):raise AssertionError('Invalid evidence publication')
        if fault in ('symlink','truncated'):
            with pytest.raises((OSError,p.RelayError)):await reconcile(journal,scope,path.parent.parent,publish)
        else:assert await reconcile(journal,scope,path.parent.parent,publish)==0
    finally:journal.close()


def test_no_eviction_of_unresolved_records_and_immutable_prepare(tmp_path):
    _,scope,record,journal,event=prepared(tmp_path)
    try:
        journal.capacity=1
        assert journal.prepare(scope,record)=='operation'
        foreign=copy.deepcopy(record);foreign['operation']['actor']['principal']='other'
        with pytest.raises(p.RelayError):journal.prepare(scope,foreign)
        foreign=copy.deepcopy(record);foreign['operation']['operationId']=foreign['event']['operationId']='second'
        with pytest.raises(p.RelayError) as error:journal.prepare(scope,foreign)
        assert error.value.code is p.ErrorCode.LIMIT
        assert len(journal.pending(scope,prepared=True))==1
    finally:journal.close()


async def test_cached_reference_still_checks_original_prefix_on_reopen(tmp_path):
    path,scope,_,journal,event=prepared(tmp_path)
    journal.sent(scope,event.tool,event.digest);journal.complete(scope,event)
    async def lost(value):raise ConnectionError()
    with pytest.raises(ConnectionError):await reconcile(journal,scope,path.parent.parent,lost)
    journal.close();path.write_text(path.read_text().replace('api-message','api-changed'))
    journal=DecisionEvidenceJournal(tmp_path/'journal')
    try:
        with pytest.raises(p.RelayError):await reconcile(journal,scope,path.parent.parent,lost)
        assert journal.pending(scope)[0][3]
    finally:journal.close()


async def test_revoked_hook_cannot_fill_original_sent_record(tmp_path):
    import asyncio
    from types import SimpleNamespace

    from acc.broker.claude_hooks import ClaudeHookServer
    from acc.session_relay.claude_hook import exchange
    path,scope,_,journal,event=prepared(tmp_path)
    journal.sent(scope,event.tool,event.digest)
    async def revoked():return False
    async def forbidden(payload):raise AssertionError('No new card, input or owner observation after revocation')
    server=ClaudeHookServer(tmp_path/'hooks',SimpleNamespace(run_id='1',binding=p.Binding(**scope)),revoked,forbidden,forbidden, accept_pid=lambda pid:pid==__import__('os').getpid())
    server.evidence_journal=journal
    payload={'session_id':event.session,'hook_event_name':'PostToolUse','tool_use_id':event.tool,
        'prompt_id':'query-1','tool_name':'Bash','tool_input':{'command':'fixture'}}
    try:
        await server.start()
        assert await asyncio.to_thread(exchange,server.directory/'owner-channel.json',payload)=={'behavior':'deny'}
        assert journal.pending(scope)[0][2] is None
        assert not server.pending and not server.tool_bindings.allowed
        assert await asyncio.to_thread(exchange,server.directory/'owner-channel.json',
            {**payload,'hook_event_name':'PermissionRequest'})=={'behavior':'deny'}
    finally:await server.close();journal.close()
