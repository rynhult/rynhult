"""Final native bytes require authority after the shared stdio write lock."""
import asyncio
from types import SimpleNamespace

import pytest
from test_session_relay_codex import BINDING, adapter, operation

from acc.broker.session_relay import CodexRuntimeOwner
from acc.session_relay import protocol as p
from acc.session_relay.adapters.base import ReviewedSpec
from acc.session_relay.adapters.codex import NativeWriteRefused, StdioRPC


class Writer:
    def __init__(self):
        self.frames = []
    def write(self, frame):
        self.frames.append(frame)
    async def drain(self):
        pass
    def close(self):
        pass
    async def wait_closed(self):
        pass


@pytest.mark.parametrize('change', ['authority', 'deadline', 'target', 'capability', 'runtime'])
async def test_native_interrupt_write_lock_cannot_preserve_stale_admission(change):
    instance, _ = await adapter()
    await instance.submit(operation())
    writer, reader = Writer(), asyncio.StreamReader()
    async def observe(event):
        pass
    rpc = StdioRPC(reader, writer, observe)
    valid = True
    def validate():
        if not valid:
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
    runtime = SimpleNamespace(spec=ReviewedSpec(BINDING, 'run1', {}), token='private-process1', validate=validate)
    instance.owner = CodexRuntimeOwner(runtime, rpc)
    allowed = True
    async def authorize(operation):
        return allowed
    instance.authorize = authorize
    await rpc.write_lock.acquire()
    pending = asyncio.create_task(instance.interrupt(operation('turn.interrupt', {'turnId': 'op1'}, operation_id='interrupt1')))
    try:
        for _ in range(100):
            if rpc.pending:
                break
            await asyncio.sleep(.01)
        assert rpc.pending and not writer.frames
        if change == 'authority':
            allowed = False
        elif change == 'deadline':
            instance.clock = lambda: 2000
        elif change == 'target':
            instance.active_turn = 'replacement-operation'
        elif change == 'capability':
            from dataclasses import replace
            instance.record = replace(instance.record, operations={})
        else:
            valid = False
        rpc.write_lock.release()
        with pytest.raises((NativeWriteRefused, p.RelayError)):
            await asyncio.wait_for(pending, 2)
        assert not writer.frames and not rpc.pending
    finally:
        if rpc.write_lock.locked():
            rpc.write_lock.release()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await rpc.close()


async def test_stdio_response_authority_runs_inside_write_lock_before_any_decision_bytes():
    reader, writer = asyncio.StreamReader(), Writer()
    async def observe(event):
        pass
    rpc = StdioRPC(reader, writer, observe)
    allowed, checks = True, []
    async def current():
        checks.append(rpc.write_lock.locked())
        return allowed
    await rpc.write_lock.acquire()
    response = asyncio.create_task(rpc.respond(7, {'decision': 'accept'}, before_write=current))
    await asyncio.sleep(0)
    allowed = False
    rpc.write_lock.release()
    try:
        with pytest.raises(NativeWriteRefused):
            await response
        assert checks == [True] and not writer.frames
    finally:
        await rpc.close()
