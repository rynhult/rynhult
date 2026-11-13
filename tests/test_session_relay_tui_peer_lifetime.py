"""Read reconnection cannot replace the kernel identity of a native view."""
import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from acc.broker.relay_enclosure import incarnation
from acc.broker.relay_tui import OwnedTUI


@pytest.mark.parametrize('state', ['disconnected', 'dead', 'closed', 'changed-incarnation', 'changed-executable', 'never-pinned'])
async def test_peer_lifetime_requires_original_live_kernel_identity(tmp_path, state):
    child = await asyncio.create_subprocess_exec(sys.executable, '-c', 'import time;time.sleep(60)')
    descriptor = os.pidfd_open(child.pid)
    try:
        view = object.__new__(OwnedTUI)
        view.closed = False
        view.peer, view.peer_fd = (child.pid, incarnation(child.pid)), descriptor
        view.executable = Path(sys.executable).resolve()
        (tmp_path / 'started.json').write_text(json.dumps({'pid': os.getpid(), 'started': incarnation(os.getpid())}))
        view.enclosure = SimpleNamespace(path=tmp_path, proof=lambda *_: True)
        view.native_socket = SimpleNamespace(bootstrapped_clients=set())
        assert not view.bootstrapped()
        if state == 'dead':
            child.kill()
            await asyncio.wait_for(child.wait(), 3)
        elif state == 'closed':view.closed = True
        elif state == 'changed-incarnation':view.peer = child.pid, 'different-start'
        elif state == 'changed-executable':view.executable = Path('/usr/bin/false').resolve()
        elif state == 'never-pinned':view.peer = None
        assert view.alive() is (state == 'disconnected')
    finally:
        os.close(descriptor)
        if child.returncode is None:
            child.kill()
            await child.wait()
