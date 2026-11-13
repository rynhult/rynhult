import asyncio
from types import SimpleNamespace

from acc.broker.relay_ownership import ExclusiveRuntime


async def test_global_factory_lock_is_not_held_during_sdk_launch():
    state=ExclusiveRuntime.__new__(ExclusiveRuntime)
    state.factory=SimpleNamespace(lock=asyncio.Lock());state.spec=None
    async def launch():
        await asyncio.wait_for(state.factory.lock.acquire(),.2)
        state.factory.lock.release()
        return {'started':True}
    state.launch=launch
    assert await state.handle('launch',{})=={'started':True}
