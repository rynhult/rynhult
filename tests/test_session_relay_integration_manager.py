"""Retiring retained runtimes cannot poison explicit new-session creation."""
import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.server.session_relay import runtime


@pytest.mark.parametrize('cached', ['absent', 'none', 'existing'])
async def test_manager_recreation_is_single_and_does_not_launch(runtime_case, monkeypatch, cached):  # noqa: F811
    c = runtime_case
    if cached == 'absent':
        if hasattr(c.ctx, 'relay_runtime'):
            del c.ctx.relay_runtime
    else:
        c.ctx.relay_runtime = c.manager if cached == 'existing' else None
    created = []
    original = runtime.RuntimeManager

    class ObservedManager(original):
        def __init__(self, ctx):
            time.sleep(.02)
            super().__init__(ctx)
            created.append(self)

    monkeypatch.setattr(runtime, 'RuntimeManager', ObservedManager)
    barrier = threading.Barrier(8)

    def request():
        barrier.wait(timeout=5)
        return runtime.manager(c.ctx)

    def concurrent_requests():
        with ThreadPoolExecutor(max_workers=8) as pool:
            return list(pool.map(lambda _: request(), range(8)))

    managers = await asyncio.to_thread(concurrent_requests)
    try:
        assert all(item is managers[0] for item in managers)
        assert c.ctx.relay_runtime is managers[0]
        assert not c.factory.active
        if cached == 'existing':
            assert managers[0] is c.manager and created == []
        else:
            assert created == [managers[0]]
            assert c.ctx.supervisor.relay_manager is managers[0]
            assert not managers[0].runs and not managers[0].launches
    finally:
        for item in created:
            await item.close()
