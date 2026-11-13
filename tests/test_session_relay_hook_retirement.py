"""Closing an observational hook must not cancel the shared authority RPC peer."""
import asyncio
import time
from types import SimpleNamespace

import pytest

from acc.broker.claude_hooks import ClaudeHookServer
from acc.session_relay.claude_hook import exchange
from acc.session_relay.runtime_peer import RuntimePeer


@pytest.mark.parametrize('responsive',[True,False])
async def test_hook_retirement_drains_observation_or_fences_stuck_authority(tmp_path,responsive):
    entered, release = asyncio.Event(), asyncio.Event()
    peers = []
    async def authority(method, params):
        entered.set()
        await release.wait()
        return True
    async def accept(reader, writer):
        peers.append(RuntimePeer(reader, writer, authority))
    server = await asyncio.start_unix_server(accept, str(tmp_path / 'authority.sock'))
    reader, writer = await asyncio.open_unix_connection(str(tmp_path / 'authority.sock'))
    peer = RuntimePeer(reader, writer, authority)
    async def authorize():
        return await peer.call('authority.owner', {})
    async def observed(payload):
        pass
    hooks = ClaudeHookServer(tmp_path / 'hooks', SimpleNamespace(run_id='run',
        binding=SimpleNamespace(native_conversation='native')), authorize, observed, observed, accept_pid=lambda pid:pid==__import__('os').getpid())
    await hooks.start()
    hooks.tool_bindings.observe({'session_id':'native','prompt_id':'prompt-1','hook_event_name':'PreToolUse',
        'tool_use_id':'tool-1','tool_name':'Bash','tool_input':{'command':'fixture'}})
    client = asyncio.create_task(asyncio.to_thread(exchange, hooks.directory / 'owner-channel.json',
        {'session_id': 'native', 'hook_event_name': 'SessionEnd'}))
    closing = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        started=time.monotonic()
        closing = asyncio.create_task(hooks.close())
        await asyncio.sleep(.05)
        assert not peer.closed and not closing.done()
        assert hooks.token=='' and not hooks.server.is_serving()
        if responsive:
            release.set()
            await asyncio.wait_for(closing, 3)
            assert await client == {'observed': True}
            assert not peer.closed
            assert await peer.call('authority.owner', {}) is True  # replacement owner still has its channel
        else:
            await asyncio.wait_for(closing, 8)
            assert time.monotonic()-started>=4.9  # real bounded drain, no patched timeout
            assert await client == {'behavior':'deny'}
            assert peer.closed  # stuck/unknown authority cannot preserve a writer
    finally:
        release.set()
        if closing:
            await asyncio.gather(closing, return_exceptions=True)
        else:
            await hooks.close()
        await asyncio.gather(client, return_exceptions=True)
        await peer.close()
        for remote in peers:
            await remote.close()
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize('sdk',[False,True])
async def test_retired_hook_cannot_release_queued_allow_or_sdk_delegation(tmp_path,sdk):
    blocked,release=asyncio.Event(),asyncio.Event()
    checks=0
    async def authorize():
        nonlocal checks
        checks+=1
        if checks==(2 if sdk else 3):
            blocked.set()
            await release.wait()
        return True
    async def observed(_):
        pass
    async def verified():
        return True
    answers=[]
    async def publish(event):
        answers.append(asyncio.create_task(hooks.answer(event['decisionId'],event['actionDigest'],'allow_once',verified)))
    hooks=ClaudeHookServer(tmp_path/'hooks',SimpleNamespace(run_id='run',
        binding=SimpleNamespace(native_conversation='native')),authorize,publish,observed,
        sdk_permission_owner=lambda:sdk, accept_pid=lambda pid:pid==__import__('os').getpid())
    await hooks.start()
    hooks.tool_bindings.observe({'session_id':'native','prompt_id':'prompt-1','hook_event_name':'PreToolUse',
        'tool_use_id':'tool-1','tool_name':'Bash','tool_input':{'command':'fixture'}})
    client=asyncio.create_task(asyncio.to_thread(exchange,hooks.directory/'owner-channel.json',
        {'session_id':'native','prompt_id':'prompt-1','hook_event_name':'PermissionRequest','tool_name':'Bash','tool_input':{'command':'fixture'}}))
    closing=None
    try:
        await asyncio.wait_for(blocked.wait(),3)
        closing=asyncio.create_task(hooks.close())
        while hooks.token:
            await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(closing,3)
        assert await client=={'behavior':'deny'}
    finally:
        release.set()
        if closing:await asyncio.gather(closing,return_exceptions=True)
        else:await hooks.close()
        await asyncio.gather(client,*answers,return_exceptions=True)
