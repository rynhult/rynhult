"""Real OS root exit with live pipe holders; no native CLI/account required."""
import asyncio
import json
import os
import select
import signal
from types import SimpleNamespace

import pytest

from acc.broker.relay_runtime import BrokerRuntime
from acc.broker.session_relay import PrivateRuntime
from acc.session_relay import protocol as p
from acc.session_relay.adapters.base import ReviewedSpec


@pytest.fixture
async def kernel_case(tmp_path):
    work=tmp_path/'work';work.mkdir()
    executable=tmp_path/'fake-vendor'
    executable.write_text('#!/usr/bin/python3\nimport os,json,time\nchild=os.fork()\n'
        'if child==0:\n time.sleep(120);os._exit(0)\n'
        'print(json.dumps({"vendor":os.getpid(),"pipeChild":child}),flush=True)\ntime.sleep(120)\n')
    executable.chmod(0o700);digest=p.sha256_digest(b'fixture')
    binding=p.Binding('binding','installation','node','account',str(work),'codex','native',digest)
    runtime=PrivateRuntime(tmp_path/'runtime',ReviewedSpec(binding,'run1',{'guard_digest':digest,'app_server_args':[]}))
    process=await runtime.start(str(executable),{'PATH':'/usr/bin:/bin'})
    info=json.loads(await asyncio.wait_for(process.stdout.readline(),3))
    fds=[os.pidfd_open(pid) for pid in {process.pid,info['vendor'],info['pipeChild']}]
    root_fd=os.pidfd_open(process.pid)
    reader=asyncio.create_task(process.stdout.read())
    state=BrokerRuntime.__new__(BrokerRuntime)
    state.owner=SimpleNamespace(runtime=runtime);state.stopping=False;state.ready=True
    state.broker=SimpleNamespace(link='active');state.writers=SimpleNamespace(close=lambda:None)
    state.peer=SimpleNamespace(invalidated=False)
    async def call(*_args):return True
    state.peer.call=call
    state.peer.invalidate=lambda:setattr(state.peer,'invalidated',True)
    async def finish(_code):
        assert not state.ready and state.broker.link is None
        state.peer.invalidate()
    state.finish_exit=finish
    watcher=asyncio.create_task(state.observe_exit())
    await asyncio.sleep(.02)
    try:yield SimpleNamespace(runtime=runtime,process=process,root_fd=root_fd,fds=fds,reader=reader,state=state,watcher=watcher)
    finally:
        watcher.cancel();await asyncio.gather(watcher,return_exceptions=True)
        for fd in fds:
            try:signal.pidfd_send_signal(fd,signal.SIGKILL)
            except ProcessLookupError:pass
        await asyncio.wait_for(reader,3);await asyncio.wait_for(process.wait(),3)
        await runtime.stop()
        for fd in [*fds,root_fd]:os.close(fd)


async def kill_root(c):
    signal.pidfd_send_signal(c.root_fd,signal.SIGKILL)
    async with asyncio.timeout(2):
        while not select.select([c.root_fd],[],[],0)[0]:await asyncio.sleep(.005)
    assert not c.reader.done(), 'The test must retain real inherited stdout pipes'


async def test_kernel_exit_fences_before_pipe_eof(kernel_case):
    c=kernel_case;await kill_root(c)
    await asyncio.wait_for(asyncio.shield(c.watcher),1)
    assert not c.state.ready and c.state.broker.link is None and c.state.peer.invalidated
    assert not c.reader.done()


async def test_runtime_validation_refuses_dead_root_without_waiting_for_observer(kernel_case):
    c=kernel_case;c.watcher.cancel();await asyncio.gather(c.watcher,return_exceptions=True)
    await kill_root(c)
    with pytest.raises(p.RelayError) as error:c.runtime.validate()
    assert error.value.code is p.ErrorCode.STALE_AUTHORITY
    assert not c.reader.done()


async def test_dead_owner_preserves_readonly_integrity_guard(kernel_case, tmp_path):
    c=kernel_case
    c.state.factory=SimpleNamespace(config=SimpleNamespace(
        folder_policy_required=False,folder_revision=None,allowed_folders=False))
    c.state.binding=SimpleNamespace(workspace=str(tmp_path))
    c.state.guards={};c.state.run_dir=tmp_path
    await kill_root(c)
    c.state.guard_check()  # retained output/evidence reads still perform integrity checks
    with pytest.raises(p.RelayError) as error:c.state.check_owner_lifetime()
    assert error.value.code is p.ErrorCode.STALE_AUTHORITY


@pytest.mark.parametrize('method',['request','notify'])
async def test_queued_native_mutation_rechecks_kernel_at_write_lock(kernel_case, method):
    from acc.broker.session_relay import CodexRuntimeOwner
    from acc.session_relay.adapters.codex import StdioRPC
    c=kernel_case;written=[]
    class Writer:
        def write(self,data):written.append(data)
        async def drain(self):pass
        def close(self):pass
    async def observe(_):pass
    rpc=StdioRPC(asyncio.StreamReader(),Writer(),observe)
    owner=CodexRuntimeOwner(c.runtime,rpc)
    assert await owner.validate_before_write() is True  # integration writer requires explicit admission
    await rpc.write_lock.acquire()
    pending=asyncio.create_task(getattr(owner,method)('turn/interrupt',{}))
    await asyncio.sleep(.02)
    assert not pending.done()
    await kill_root(c)
    rpc.write_lock.release()
    try:
        with pytest.raises(p.RelayError) as error:await asyncio.wait_for(pending,1)
        assert error.value.code is p.ErrorCode.STALE_AUTHORITY
        assert written==[]
    finally:await rpc.close()
