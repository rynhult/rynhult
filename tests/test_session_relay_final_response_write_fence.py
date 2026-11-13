"""Authority is rechecked inside the actual response-write serialization barrier."""
import asyncio

import pytest
from test_session_relay_codex import adapter, operation

from acc.session_relay import protocol as p
from acc.session_relay.adapters.codex import NativeWriteRefused, StdioRPC


class Writer:
    def __init__(self):self.frames=[]
    def write(self,data):self.frames.append(data)
    async def drain(self):pass
    def close(self):pass


async def rpc_case():
    async def observe(_):pass
    writer=Writer();rpc=StdioRPC(asyncio.StreamReader(),writer,observe)
    return rpc,writer


async def test_guard_runs_under_write_lock_and_rejects_revoked_authority():
    rpc,writer=await rpc_case();entered=asyncio.Event();current=True
    original=rpc._write
    async def arrival(message,**options):
        entered.set();return await original(message,**options)
    rpc._write=arrival
    async def verify():
        assert rpc.write_lock.locked()
        if not current:raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
    await rpc.write_lock.acquire()
    try:
        pending=asyncio.create_task(rpc.respond(1,{'decision':'accept'},before_write=verify))
        await asyncio.wait_for(entered.wait(),1)
        current=False;rpc.write_lock.release()
        with pytest.raises(p.RelayError):await pending
        assert writer.frames==[]
    finally:
        if rpc.write_lock.locked():rpc.write_lock.release()
        await rpc.close()


async def test_transport_closing_during_awaited_final_guard_never_writes():
    rpc,writer=await rpc_case();entered=asyncio.Event();release=asyncio.Event()
    async def verify():entered.set();await release.wait();return True
    pending=asyncio.create_task(rpc.respond(1,{'decision':'accept'},before_write=verify))
    try:
        await asyncio.wait_for(entered.wait(),1)
        rpc.closed=True;release.set()
        with pytest.raises(NativeWriteRefused):await pending
        assert writer.frames==[]
    finally:await rpc.close()


@pytest.mark.parametrize('guarded', [False, True])
async def test_adapter_rechecks_after_transport_lock_without_reclaiming_operation(guarded):
    from dataclasses import replace
    instance,owner=await adapter();await instance.submit(operation())
    instance.record=replace(instance.record,operations={**instance.record.operations,
        'decision.tool_approval':p.CapabilityEntry(p.Support.QUALIFIED,evidence=('synthetic fixture only',))})
    rpc,writer=await rpc_case();owner.respond=rpc.respond
    if guarded:
        async def guarded_reply(identifier, value, before_write):
            await rpc.respond(identifier, value, before_write=before_write)
        owner.respond_guarded = guarded_reply
    current=True
    async def authorize(_):return current
    instance.authorize=authorize
    instance.register_decision('q',native_request={'id':42,'method':'item/commandExecution/requestApproval',
        'params':{'threadId':'native1','turnId':'native-turn1','itemId':'item1'}},action_digest=p.sha256_digest(b'action'),revision=1,expires_ms=2000)
    params={'decisionId':'q','choiceId':'accept','expectedRevision':1,'actionDigest':p.sha256_digest(b'action')}
    op=operation('decision.reply',params,operation_id='answer')
    arrived=asyncio.Event();original=rpc._write
    async def arrival(message,**options):arrived.set();return await original(message,**options)
    rpc._write=arrival
    await rpc.write_lock.acquire()
    try:
        pending=asyncio.create_task(instance.reply_decision(op))
        await asyncio.wait_for(arrived.wait(),1)
        assert op.operation_id in instance.attempted
        current=False;rpc.write_lock.release()
        with pytest.raises(p.RelayError):await pending
        assert not writer.frames and not instance.decisions['q']['sent']
        with pytest.raises(p.RelayError):await instance.reply_decision(op)
        assert not writer.frames
    finally:
        if rpc.write_lock.locked():rpc.write_lock.release()
        await rpc.close()
