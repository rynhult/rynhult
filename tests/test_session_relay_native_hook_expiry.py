"""Native hook absolute deadline and unanswered expiry publication (unit clock)."""
import asyncio
import os
from types import SimpleNamespace

import pytest

from acc.broker import claude_hooks
from acc.session_relay.claude_hook import exchange


@pytest.mark.parametrize('phase',['publication','timer','chosen_late'])
async def test_native_hook_expires_only_unanswered_original_question(tmp_path,monkeypatch,phase):
    clock=[1000.0]
    monkeypatch.setattr(claude_hooks,'time',SimpleNamespace(monotonic=lambda:clock[0],time_ns=lambda:int(clock[0]*1e9)))
    original_wait=asyncio.wait_for
    async def wait(awaitable,timeout):
        if phase=='timer' and isinstance(awaitable,asyncio.Future):
            clock[0]+=16
            raise TimeoutError
        return await original_wait(awaitable,timeout)
    monkeypatch.setattr(claude_hooks.asyncio,'wait_for',wait)
    events=[]
    checks=0
    async def authorize():
        nonlocal checks
        checks+=1
        if phase=='chosen_late' and checks==3:clock[0]+=16
        return True
    async def observe(_):pass
    async def verify():return True
    async def publish(event):
        events.append(event)
        if event['kind']=='decision':
            if phase=='publication':clock[0]+=16
            if phase=='chosen_late':await hooks.answer(event['decisionId'],event['actionDigest'],'allow_once',verify)
    spec=SimpleNamespace(run_id='run',binding=SimpleNamespace(native_conversation='native'))
    hooks=claude_hooks.ClaudeHookServer(tmp_path/'hooks',spec,authorize,publish,observe,
        accept_pid=lambda pid: pid == os.getpid())  # this unit's controlled sender stands in for the pinned owner
    await hooks.start()
    hooks.tool_bindings.observe({'session_id':'native','prompt_id':'prompt-1','hook_event_name':'PreToolUse',
        'tool_use_id':'tool-1','tool_name':'Bash','tool_input':{'command':'fixture'}})
    try:
        result=await asyncio.to_thread(exchange,hooks.directory/'owner-channel.json',
            {'session_id':'native','prompt_id':'prompt-1','hook_event_name':'PermissionRequest','tool_name':'Bash','tool_input':{'command':'fixture'}})
        assert result=={'behavior':'deny'}
        assert [event['kind'] for event in events]==(['decision'] if phase=='chosen_late' else ['decision','decision.expired'])
        assert events[0]['expiresAtMs']==1_015_000
        if phase!='chosen_late':assert events[1]['actionDigest']==events[0]['actionDigest']
        assert not hooks.pending
    finally:await hooks.close()
