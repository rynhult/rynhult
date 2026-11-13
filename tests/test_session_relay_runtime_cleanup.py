"""Bounded R1 resource/EOF faults through production runtime cleanup."""
import asyncio
import os
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from acc.broker.relay_runtime import BrokerRuntime, RuntimeTransportFactory
from acc.broker.session_relay import AdmissionJournal, PayloadStaging


def runtime():
    factory = RuntimeTransportFactory(SimpleNamespace(), owned_view_factory=None)
    state = BrokerRuntime(factory, 'fixture-enrollment')
    state.key = ('fixture', 'run1')
    factory.active[state.key] = state
    return state


@pytest.mark.parametrize('fault', ['attachment', 'view', 'socket', 'owner', 'staging'])
async def test_cleanup_fault_still_releases_remaining_owned_resources(fault):
    state = runtime()
    calls = []
    async def release(name):
        calls.append(name)
        if name == fault:
            raise RuntimeError('artificial cleanup fault')
        return True
    def sync_release(name):
        calls.append(name)
        if name == fault:
            raise RuntimeError('artificial cleanup fault')
    state.terminal_host = SimpleNamespace(run='fixture', detach=lambda _: release('attachment'))
    state.owned_view = SimpleNamespace(close=lambda: release('view'))
    state.native_socket = SimpleNamespace(close=lambda: release('socket'))
    state.owner = SimpleNamespace(stop=lambda: release('owner'))
    state.staging = SimpleNamespace(close=lambda: sync_release('staging'))
    state.journal = SimpleNamespace(close=lambda: sync_release('journal'))
    with pytest.raises(RuntimeError, match='artificial cleanup fault'):
        await state.close()
    assert calls == ['attachment', 'view', 'socket', 'owner', 'staging', 'journal']
    assert not state.factory.active
    with pytest.raises(RuntimeError):
        await state.close()
    assert len(calls) == 6  # close/waiter retry cannot double-close descriptors


async def test_cancelled_cleanup_waiter_cannot_strand_owner_or_remove_replacement():
    state = runtime()
    entered, release = asyncio.Event(), asyncio.Event()
    async def delayed():
        entered.set()
        await release.wait()
        return True
    state.owned_view = SimpleNamespace(close=delayed)
    state.owner = SimpleNamespace(stop=AsyncMock())
    waiter = asyncio.create_task(state.close())
    await entered.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    replacement = object()
    state.factory.active[state.key] = replacement
    release.set()
    await state.close()
    state.owner.stop.assert_awaited_once()
    assert state.factory.active[state.key] is replacement


async def test_2049_runtime_lifetimes_reclaim_real_journal_staging_and_registry(tmp_path):
    factory = RuntimeTransportFactory(SimpleNamespace(), owned_view_factory=None)
    for generation in range(2049):
        state = BrokerRuntime(factory, 'fixture-enrollment')
        state.key = ('fixture', str(generation))
        factory.active[state.key] = state
        root = tmp_path / str(generation)
        state.journal = AdmissionJournal(root / 'journal')
        state.staging = PayloadStaging(root / 'payloads')
        descriptors = (state.journal.fd, state.staging.fd)
        state.owner = SimpleNamespace(stop=AsyncMock())
        await state.close()
        assert not factory.active
        state.owner.stop.assert_awaited_once()
        for descriptor in descriptors:
            with pytest.raises(OSError):
                os.fstat(descriptor)
        # The durable receipt store remains readable after actual connection close.
        with pytest.raises(sqlite3.ProgrammingError, match='closed database'):
            state.journal.db.execute('SELECT 1')


async def test_unknown_view_exit_is_reported_after_owner_is_stopped():
    from acc.session_relay import protocol as p
    state = runtime()
    state.owned_view = SimpleNamespace(close=AsyncMock(return_value=False))
    state.owner = SimpleNamespace(stop=AsyncMock())
    with pytest.raises(p.RelayError) as error:
        await state.close()
    assert error.value.code is p.ErrorCode.INTEGRITY_FAILURE
    state.owner.stop.assert_awaited_once()
    assert not state.factory.active


async def test_retained_client_cleanup_fault_cannot_strand_fresh_or_retired_processes():
    state = runtime()
    calls = []
    async def detach(index):
        calls.append(('detach', index))
        if index == 0:
            raise RuntimeError('artificial retired bridge failure')
    async def close(index):
        calls.append(('close', index))
        return True
    state.owned_generations = [(SimpleNamespace(close=lambda i=i: close(i)),
        SimpleNamespace(run='fixture', detach=lambda _, i=i: detach(i))) for i in range(3)]
    state.owned_view, state.terminal_host = state.owned_generations[-1]
    state.owner = SimpleNamespace(stop=AsyncMock())
    with pytest.raises(RuntimeError, match='retired bridge'):
        await state.close()
    assert calls == [('detach', i) for i in range(3)] + [('close', i) for i in range(3)]
    state.owner.stop.assert_awaited_once()
