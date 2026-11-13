import asyncio
import json
import os
import signal
import sys
from types import SimpleNamespace

from acc.broker.relay_enclosure import Enclosure, incarnation


async def test_same_uid_disk_writer_cannot_forge_empty_tree_after_supervisor_death(tmp_path):
    executable=tmp_path/'vendor'
    executable.write_text(f'#!{sys.executable}\nimport time;time.sleep(60)\n');executable.chmod(0o500)
    enclosure=Enclosure(tmp_path/'private',SimpleNamespace(run_id='run'),executable)
    process=await asyncio.create_subprocess_exec(str(enclosure.cli),stdout=asyncio.subprocess.DEVNULL)
    identity=f'{process.pid}:{incarnation(process.pid)}';owned=[]
    try:
        async with asyncio.timeout(4):
            while not enclosure.proof('started.json',identity):await asyncio.sleep(.01)
        async with asyncio.timeout(4):
            while not owned:
                children=open(f'/proc/{process.pid}/task/{process.pid}/children').read().split()
                owned=[os.pidfd_open(int(pid)) for pid in children]
                if not owned:await asyncio.sleep(.01)
        process.kill();await process.wait()
        forged={**json.loads((enclosure.path/'started.json').read_text()),'empty':True,'status':0}
        (enclosure.path/'exit.json').write_text(json.dumps(forged))
        (enclosure.path/'exit.json').chmod(0o600)
        assert not enclosure.proof('exit.json',identity),'Same-uid disk forgery became positive native cleanup'
        assert enclosure.native_status(identity) is None
    finally:
        for fd in owned:
            try:signal.pidfd_send_signal(fd,signal.SIGKILL)
            except ProcessLookupError:pass
            os.close(fd)
        if process.returncode is None:process.kill();await process.wait()
        enclosure.close()
