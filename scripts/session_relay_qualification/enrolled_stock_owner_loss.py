"""Signal only the pinned vendor child, preserving its real enclosure supervisor."""
import asyncio
import copy
import hashlib
import os
import select
import signal
from pathlib import Path


def identity(pid):
    fields=Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()
    return int(fields[1]),fields[19]


async def exited(fd):
    poll=select.poll();poll.register(fd,select.POLLIN)
    async with asyncio.timeout(10):
        while not poll.poll(0):await asyncio.sleep(.01)


async def terminate_native(state,release):
    from acc.broker.relay_runtime import CLI_DIGEST
    runtime=state.owner.runtime;process=runtime.process
    assert process.returncode is None
    enclosure=getattr(runtime,'enclosure',None);reader=None
    supervisor_fd=os.pidfd_open(process.pid);native_fd=None
    try:
        supervisor_started=identity(process.pid)[1]
        assert supervisor_started==runtime.token.split(':')[1]
        if enclosure is not None:
            reader=copy.copy(enclosure);reader.fd=os.dup(enclosure.fd)
            assert reader.proof('started.json',runtime.token)
            children=Path(f'/proc/{process.pid}/task/{process.pid}/children').read_text().split()
            candidates=[]
            for child in children:
                pid=int(child)
                try:
                    parent,started=identity(pid)
                    digest='sha256:'+hashlib.sha256(Path(f'/proc/{pid}/exe').read_bytes()).hexdigest()
                except (OSError,ProcessLookupError):continue
                if parent==process.pid and digest==CLI_DIGEST:candidates.append((pid,started))
            assert len(candidates)==1,('Expected exactly one pinned owned vendor child',candidates)
            native_pid,native_started=candidates[0]
        else:native_pid,native_started=process.pid,supervisor_started
        native_fd=os.pidfd_open(native_pid)
        parent,started=identity(native_pid)
        assert started==native_started
        assert enclosure is None or parent==process.pid
        assert 'sha256:'+hashlib.sha256(Path(f'/proc/{native_pid}/exe').read_bytes()).hexdigest()==CLI_DIGEST
        signal.pidfd_send_signal(native_fd,signal.SIGKILL)
        await exited(native_fd)
        # Parent kernel exit is distinct from asyncio Process.wait(), which also
        # waits for inherited pipe cleanup. Never kill the subreaper supervisor.
        await exited(supervisor_fd)
        async with asyncio.timeout(10):
            while not state.owner.rpc.closed:await asyncio.sleep(.01)
        release()
        await asyncio.wait_for(process.wait(),10)
        if reader is not None:
            assert reader.proof('exit.json',runtime.token)
            native_status=reader.native_status(runtime.token)
            assert native_status==-signal.SIGKILL
        else:native_status=process.returncode
        return {'supervisorPid':process.pid,'supervisorStarted':supervisor_started,
            'nativePid':native_pid,'nativeStarted':native_started,'nativeExecutableDigest':CLI_DIGEST,
            'supervisorPreservedForCleanup':enclosure is not None,'nativeKernelExitProven':True,
            'supervisorKernelExitProven':True,'stdioEofBeforeBarrierRelease':True,
            'emptyEnclosureProven':reader is not None,'nativeExitStatus':native_status,
            'supervisorReturnCode':process.returncode}
    finally:
        if native_fd is not None:os.close(native_fd)
        os.close(supervisor_fd)
        if reader is not None:reader.close()
