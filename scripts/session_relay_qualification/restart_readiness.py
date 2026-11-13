"""Read-only readiness for one exact restarted fixture process."""
import asyncio


async def wait_for_worker_state(rpc, endpoint, expected_pid, *, timeout=3):
    # A bound pathname is visible before listen(). Retry only that transport
    # boundary, never an application error or a mutating operation.
    async with asyncio.timeout(timeout):
        while True:
            try:
                state = await rpc(endpoint, 'state')
            except (FileNotFoundError, ConnectionRefusedError):
                await asyncio.sleep(.01)
                continue
            assert state['pid'] == expected_pid, 'Unexpected worker incarnation'
            return state
