import asyncio
from types import SimpleNamespace

import pytest

from acc.broker.claude_hooks import ClaudeHookServer
from acc.session_relay.claude_hook import exchange


@pytest.mark.parametrize('allowed',[False,True])
@pytest.mark.parametrize('event',['PostToolUse','PermissionRequest','Stop','SessionEnd'])
async def test_same_uid_token_holder_without_pinned_owner_ancestry_has_no_effect(tmp_path,event,allowed):
    observed=[];journal=[];published=[]
    async def authorized():return allowed
    async def observe(value):observed.append(value)
    async def publish(value):
        published.append(value)
        for pending in server.pending.values():
            if not pending['future'].done():pending['future'].set_result('deny')
    spec=SimpleNamespace(run_id='run',binding=SimpleNamespace(native_conversation='native'))
    server=ClaudeHookServer(tmp_path/'hook',spec,authorized,publish,observe)
    server.evidence_journal=SimpleNamespace(observe=lambda *args:journal.append(args))
    await server.start()
    try:
        reply=await asyncio.to_thread(exchange,server.directory/'owner-channel.json',{
            'session_id':'native','hook_event_name':event,'tool_use_id':'tool','tool_name':'Bash','tool_input':{'command':'true'}})
        assert reply=={'behavior':'deny'}
        assert not observed and not journal and not published and not server.pending
    finally:await server.close()


async def test_revocation_during_observation_cannot_reach_evidence_journal(tmp_path):
    allowed=[True];journal=[]
    async def authorize():return allowed[0]
    async def observe(_):allowed[0]=False
    async def publish(_):raise AssertionError('No new card')
    spec=SimpleNamespace(run_id='run',binding=SimpleNamespace(native_conversation='native'))
    server=ClaudeHookServer(tmp_path/'hook',spec,authorize,publish,observe)
    server.accept_pid=lambda pid:pid==__import__('os').getpid()  # explicit unit fixture owner
    server.evidence_journal=SimpleNamespace(observe=lambda *args:journal.append(args))
    await server.start()
    try:
        result=await asyncio.to_thread(exchange,server.directory/'owner-channel.json',{
            'session_id':'native','hook_event_name':'PostToolUse','tool_use_id':'tool','tool_name':'Bash','tool_input':{'command':'true'}})
        assert result=={'behavior':'deny'} and not journal
    finally:await server.close()
