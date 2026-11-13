"""Durable proof resources and service/partial factories share conclusive cleanup."""
from types import SimpleNamespace

import pytest

from acc.broker.relay_ownership import ExclusiveRuntime
from acc.broker.relay_runtime import RuntimeTransportFactory


@pytest.mark.parametrize('fault', ['controller', 'partial-factory', 'proof'])
async def test_exclusive_cleanup_keeps_every_worker_release_guarantee(fault):
    factory = RuntimeTransportFactory(SimpleNamespace(), owned_view_factory=None)
    state = ExclusiveRuntime(factory, 'fixture-enrollment')
    state.key = ('fixture', 'run1')
    factory.active[state.key] = state
    state.service_operations['service'] = 'original-stage'
    calls = []

    def release(name):
        calls.append(name)
        if name == fault:
            raise RuntimeError('cleanup fault: ' + name)

    async def async_release(name):
        release(name)

    state.controller = SimpleNamespace(close=lambda: async_release('controller'))
    state.owners = SimpleNamespace(close=lambda: async_release('partial-factory'))
    state.application_proofs = SimpleNamespace(close=lambda: release('proof'))
    state.staging = SimpleNamespace(close=lambda: release('staging'))
    state.journal = SimpleNamespace(close=lambda: release('journal'))
    with pytest.raises(RuntimeError, match=fault):
        await state.close()
    assert calls == ['controller', 'partial-factory', 'proof', 'staging', 'journal']
    assert not state.service_operations and not factory.active
    assert state.denial_observers_closed
    with pytest.raises(RuntimeError, match=fault):
        await state.close()
    assert len(calls) == 5
