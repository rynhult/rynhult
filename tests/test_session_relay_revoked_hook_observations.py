"""Late evidence and stopping are observations, never fresh execution authority."""
import asyncio
import io
import json
import sys
from types import SimpleNamespace

import pytest
from test_session_relay_claude_decision_journal import prepared

from acc.broker.claude_hooks import ClaudeHookServer
from acc.session_relay import claude_hook


@pytest.mark.parametrize('failure',['false','exception'])
@pytest.mark.parametrize('sent',[False,True])
async def test_authentication_failure_cannot_journal_completion(tmp_path,failure,sent):
    _,scope,record,journal,event=prepared(tmp_path)
    if sent:journal.sent(scope,event.tool,event.digest)
    async def authorize():
        if failure=='exception':raise ConnectionError('Runtime authority refused')
        return False
    async def forbidden(*_):raise AssertionError('Observation must not publish or certify owner idle')
    server=ClaudeHookServer(tmp_path/'hook',SimpleNamespace(run_id='1',binding=SimpleNamespace(**scope)),authorize,forbidden,forbidden, accept_pid=lambda pid:pid==__import__('os').getpid())
    server.evidence_journal=journal
    await server.start()
    try:
        payload={'session_id':'native-fixture','prompt_id':'query-1','tool_name':'Bash',
            'tool_input':{'command':'fixture'},'tool_use_id':'tool-1','hook_event_name':'PostToolUse'}
        assert await asyncio.to_thread(claude_hook.exchange,server.directory/'owner-channel.json',payload)=={'behavior':'deny'}
        assert journal.pending(scope,prepared=True)[0][2] is None
        for event_name in ('Stop','SessionEnd'):
            assert await asyncio.to_thread(claude_hook.exchange,server.directory/'owner-channel.json',
                {**payload,'hook_event_name':event_name})=={'observed':True}
        assert await asyncio.to_thread(claude_hook.exchange,server.directory/'owner-channel.json',
            {**payload,'hook_event_name':'PreToolUse'})=={'behavior':'deny'}
        assert not server.pending and not server.tool_bindings.active
    finally:await server.close();journal.close()


@pytest.mark.parametrize('event',['Stop','SubagentStop','SessionEnd','PreToolUse','PermissionRequest'])
def test_unavailable_stop_observation_never_requests_another_native_turn(monkeypatch,capsys,event):
    monkeypatch.setattr(sys,'stdin',SimpleNamespace(buffer=io.BytesIO(json.dumps({'hook_event_name':event}).encode())))
    def missing(*_):raise ConnectionError('Broker unavailable')
    monkeypatch.setattr(claude_hook,'exchange',missing)
    monkeypatch.setattr(sys,'argv',['hook','/tmp/fixture/owner-channel.json'])
    assert claude_hook.main()==(0 if event in ('Stop','SubagentStop','SessionEnd','PermissionRequest') else 2)
    if event=='PermissionRequest':
        assert json.loads(capsys.readouterr().out)['hookSpecificOutput']['decision']['behavior']=='deny'
