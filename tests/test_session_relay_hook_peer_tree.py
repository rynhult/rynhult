"""Real PID ancestry for hook admission; fake vendor only, no account or CLI."""
import asyncio
import os
import signal
from types import SimpleNamespace

import pytest

from acc.broker.claude_owner import ClaudeOwnerFactory


@pytest.mark.parametrize('startup',[False,True])
async def test_only_pinned_native_tree_can_use_hook_token(tmp_path,startup):
    process=await asyncio.create_subprocess_exec('/usr/bin/python3','-c',
        'import os,time; child=os.fork(); print(child,flush=True) if child else None; time.sleep(60)',
        stdout=asyncio.subprocess.PIPE)
    child=int(await asyncio.wait_for(process.stdout.readline(),3))
    descriptors=[os.pidfd_open(pid) for pid in [process.pid,child]]
    owner=ClaudeOwnerFactory.__new__(ClaudeOwnerFactory)
    owner.current=SimpleNamespace(process=None if startup else process,
        client=SimpleNamespace(_transport=SimpleNamespace(_process=process)))
    owner.enclosure=SimpleNamespace(proof=lambda *_:True)
    try:
        assert owner._accepts_pid(child) and owner._accepts_pid(process.pid)
        assert not owner._accepts_pid(os.getpid()),'Same-UID parent is not a native descendant'
        owner.enclosure.proof=lambda *_:False
        assert not owner._accepts_pid(child),'Unattested started receipt cannot establish hook ancestry'
        owner.enclosure=None
        assert not owner._accepts_pid(child)
    finally:
        for descriptor in descriptors:
            try:signal.pidfd_send_signal(descriptor,signal.SIGKILL)
            except ProcessLookupError:pass
            os.close(descriptor)
        await asyncio.wait_for(process.wait(),3)
