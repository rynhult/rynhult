"""The verified SDK callback owns SDK decisions; native TUI hooks still deny."""
import asyncio
import io
from types import SimpleNamespace

import pytest

from acc.broker.claude_hooks import ClaudeHookServer
from acc.session_relay import claude_hook


@pytest.mark.parametrize('mode',['sdk','owner_changed','revoked','tui'])
async def test_permission_source_is_current_and_rechecked_after_authority(tmp_path,mode):
    state={'sdk':mode!='tui','checks':0}
    cards=[]
    async def authorize():
        state['checks']+=1
        if state['checks']==2:
            if mode=='owner_changed':state['sdk']=False
            if mode=='revoked':return False
        return True
    async def observe(_):
        pass
    async def verify():
        return True
    async def publish(event):
        cards.append(event)
        await hooks.answer(event['decisionId'],event['actionDigest'],'deny',verify)
    spec=SimpleNamespace(run_id='run',binding=SimpleNamespace(native_conversation='native'))
    hooks=ClaudeHookServer(tmp_path/'hooks',spec,authorize,publish,observe,
        sdk_permission_owner=lambda:state['sdk'], accept_pid=lambda pid:pid==__import__('os').getpid())
    await hooks.start()
    try:
        reply=await asyncio.to_thread(claude_hook.exchange,hooks.directory/'owner-channel.json',
            {'session_id':'native','hook_event_name':'PermissionRequest','tool_name':'Bash','tool_input':{'command':'fixture'}})
        assert reply==({'sdkPermissionOwner':True} if mode=='sdk' else {'behavior':'deny'})
        assert len(cards)==(1 if mode=='tui' else 0)
        assert not hooks.pending
    finally:
        await hooks.close()


@pytest.mark.parametrize('reply',[{'sdkPermissionOwner':True},{'behavior':'deny'}])
def test_hook_delegates_only_authenticated_sdk_reply_and_otherwise_prints_explicit_deny(monkeypatch,capsys,reply):
    monkeypatch.setattr(claude_hook,'exchange',lambda *_:reply)
    monkeypatch.setattr(claude_hook.sys,'stdin',SimpleNamespace(buffer=io.BytesIO(b'{"hook_event_name":"PermissionRequest"}')))
    monkeypatch.setattr(claude_hook.sys,'argv',['fixture','generated-channel'])
    assert claude_hook.main()==0
    output=capsys.readouterr().out
    if reply=={'sdkPermissionOwner':True}:assert output==''
    else:assert '"behavior": "deny"' in output
