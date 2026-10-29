"""Cancelled supervisor stages still join their worker threads at teardown."""
import asyncio
import threading

import pytest

from acc.server import supervisor


async def test_cancelled_stage_drains_worker_failure(supervisor_threads):
    started, release = threading.Event(), threading.Event()

    def worker():
        started.set()
        if not release.wait(5):
            raise TimeoutError('test did not release worker')
        raise RuntimeError('background stage failed')

    caller = asyncio.create_task(supervisor.asyncio.to_thread(worker))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
    finally:
        release.set()
    # The cancelled caller cannot consume the result as the normal stage handler would.
    results = await asyncio.gather(*supervisor_threads, return_exceptions=True)
    assert len(results) == 1
    assert isinstance(results[0], RuntimeError)
    assert str(results[0]) == 'background stage failed'
    # The fixture must drain this task again without turning its handled error into a teardown failure.
