"""OS-process fault: hung cleanup preserves a real child without removing fencing."""
import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from acc.broker.relay_enclosure import Enclosure, incarnation


def helper():
    path = Path(__file__).resolve().parents[1] / 'scripts/session_relay_qualification/surviving_enclosure.py'
    spec = importlib.util.spec_from_file_location('surviving_enclosure', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('fault', ['hung-cleanup', 'wrong-incarnation', 'wrong-executable'])
async def test_broker_death_remains_fenced_and_exact_native_child_can_outlive_hung_supervisor(tmp_path, fault):
    module = helper()
    broker = await asyncio.create_subprocess_exec(sys.executable, '-c', 'import time;time.sleep(60)')
    production = Enclosure(tmp_path / 'private', SimpleNamespace(run_id='run1'), sys.executable,
        lifetime={'pid': broker.pid, 'started': incarnation(broker.pid)})
    module.inject_cleanup_delay(production)
    supervisor = await asyncio.create_subprocess_exec(str(production.cli), '-c', 'import time;time.sleep(60)',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    enclosure = None
    try:
        async with asyncio.timeout(3):
            while True:
                children = Path(f'/proc/{supervisor.pid}/task/{supervisor.pid}/children').read_text().split()
                if len(children) == 1 and Path(f'/proc/{children[0]}/exe').resolve() == Path(sys.executable).resolve():
                    break
                await asyncio.sleep(.01)
        value = {'child': int(children[0])}
        started = module.process_identity(supervisor.pid)[0]
        if fault != 'hung-cleanup':
            with pytest.raises(AssertionError):
                module.SurvivingEnclosure(supervisor.pid,
                    'wrong' if fault == 'wrong-incarnation' else started,
                    '/usr/bin/false' if fault == 'wrong-executable' else sys.executable, production.path)
        else:
            enclosure = module.SurvivingEnclosure(supervisor.pid, started, sys.executable, production.path)
            proof = await enclosure.pause()
            assert proof['nativePid'] == value['child'] and proof['nativePid'] != supervisor.pid
            broker.kill()
            await asyncio.wait_for(broker.wait(), 3)
            await asyncio.sleep(.03)
            assert enclosure.check() == proof
            assert (production.path / 'qualification-held').read_text() == f'{supervisor.pid}:{started}'
            enclosure.resume()
            await enclosure.exited()
        if broker.returncode is None:
            broker.kill()
            await broker.wait()
        await asyncio.wait_for(supervisor.wait(), 5)
        assert production.proof('exit.json', f'{supervisor.pid}:{started}')
    finally:
        if enclosure:
            enclosure.close()
        if broker.returncode is None:
            broker.kill()
            await broker.wait()
        if supervisor.returncode is None:
            await asyncio.wait_for(supervisor.wait(), 5)
