"""Broker lifetime pinning: no PID-reuse authority and no orphan view tree."""
import asyncio
import os
import sys
from types import SimpleNamespace

import pytest
from test_session_relay_enclosure import until

from acc.broker.relay_enclosure import Enclosure, incarnation


@pytest.mark.parametrize('wrong_incarnation', [False, True])
async def test_owned_view_lifetime_follows_pinned_broker_not_pid(tmp_path, wrong_incarnation):
    broker = await asyncio.create_subprocess_exec(sys.executable, '-c', 'import time; time.sleep(30)',
        env={'PATH': '/usr/bin:/bin'})
    script = tmp_path / 'native-fixture'
    marker = tmp_path / 'child'
    script.write_text(f'#!{sys.executable}\nimport os,sys,time\nopen(sys.argv[1],"w").write(str(os.getpid()))\ntime.sleep(30)\n')
    script.chmod(0o500)
    enclosure = Enclosure(tmp_path / 'private', SimpleNamespace(run_id='fixture'), script,
        lifetime={'pid': broker.pid, 'started': 'wrong' if wrong_incarnation else incarnation(broker.pid)})
    process = await asyncio.create_subprocess_exec(str(enclosure.cli), str(marker), env={'PATH': '/usr/bin:/bin'},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    identity = f'{process.pid}:{incarnation(process.pid)}'
    try:
        if wrong_incarnation:
            assert await asyncio.wait_for(process.wait(), 5) != 0
            assert not marker.exists()
            assert not enclosure.proof('started.json', identity)
        else:
            await until(lambda: marker.exists() and marker.stat().st_size > 0)
            child = int(marker.read_text())
            broker.kill()
            await broker.wait()
            await asyncio.wait_for(process.wait(), 5)
            assert enclosure.proof('exit.json', identity)
            with pytest.raises(ProcessLookupError):
                os.kill(child, 0)
    finally:
        if broker.returncode is None:
            broker.kill()
        await broker.wait()
        if process.returncode is None:
            process.terminate()
        await process.wait()
        enclosure.close()
