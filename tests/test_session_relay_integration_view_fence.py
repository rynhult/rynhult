"""A live producer cannot authorize decisions from a dead native view."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from acc.broker import relay_runtime


@pytest.mark.parametrize('death', ['before', 'during', 'alive'])
async def test_final_decision_verifier_checks_view_before_and_after_main_authority(monkeypatch, death):
    state = relay_runtime.BrokerRuntime.__new__(relay_runtime.BrokerRuntime)
    operation = SimpleNamespace(fence='fence', method='decision.reply', operation_id='operation')
    live = [death != 'before']
    state.owner = None  # synthetic owner; real constructors initialize this field
    state.ready = True
    state.link_id = 'link'
    state.grant = 'grant'
    state.broker = SimpleNamespace(link=('fence', 'link'))
    state.guard_check = Mock()
    state.check_writer = Mock()
    state.operation_origin = {}
    state.owned_view = SimpleNamespace(bootstrapped=lambda: live[0])
    async def authority(*_args):
        if death == 'during':
            live[0] = False
        return True
    state.peer = SimpleNamespace(closed=False, call=AsyncMock(side_effect=authority))
    monkeypatch.setattr(relay_runtime, 'operation_wire', lambda _: {})
    assert await state.verify(operation) is (death == 'alive')
    assert state.peer.call.await_count == (0 if death == 'before' else 1)


@pytest.mark.parametrize('origin', ['chat', 'terminal'])
@pytest.mark.parametrize('lifetime', ['live-reconnecting', 'dead-before', 'dead-during'])
async def test_shared_review_distinguishes_reconnecting_reader_from_native_view_death(monkeypatch, origin, lifetime):
    state = relay_runtime.BrokerRuntime.__new__(relay_runtime.BrokerRuntime)
    operation = SimpleNamespace(fence='fence', method='decision.reply', operation_id='operation')
    live = [lifetime != 'dead-before']
    state.owner = None  # synthetic owner; real constructors initialize this field
    state.ready = True
    state.link_id = 'link'
    state.grant = 'grant'
    state.broker = SimpleNamespace(link=('fence', 'link'))
    state.guard_check = Mock()
    state.check_writer = Mock()
    state.operation_origin = {'operation': origin}
    state.owned_view = SimpleNamespace(bootstrapped=lambda: False, alive=lambda: live[0])
    async def authority(*_args):
        if lifetime == 'dead-during':live[0] = False
        return True
    state.peer = SimpleNamespace(closed=False, call=AsyncMock(side_effect=authority))
    monkeypatch.setattr(relay_runtime, 'operation_wire', lambda _: {})
    assert await state.verify(operation) is (origin == 'chat' and lifetime == 'live-reconnecting')
