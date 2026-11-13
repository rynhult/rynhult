"""Independently developed private evidence readers retain distinct protocols."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from acc.broker import application_reader, claude_reconciliation, relay_runtime


@pytest.mark.parametrize(('version', 'module', 'name'), [
    (4, application_reader, 'ApplicationReader'), (5, claude_reconciliation, 'ReadOnlyEvidence'),
])
async def test_both_private_readonly_protocols_route_without_execution(monkeypatch, version, module, name):
    selected = []
    class State:
        def __init__(self, factory, token):
            selected.append((factory, token))
        handle = AsyncMock()
        lost = lambda *_: None
        close = AsyncMock()
    monkeypatch.setattr(module, name, State)
    class Peer:
        def __init__(self, *_args, **_kwargs):
            self.task = asyncio.get_running_loop().create_future()
            self.task.set_result(None)
        close = AsyncMock()
    monkeypatch.setattr(relay_runtime, 'RuntimePeer', Peer)
    def forbidden(*_args, **_kwargs):
        raise AssertionError('read-only enrollment constructed an execution runtime')
    monkeypatch.setattr(relay_runtime, 'BrokerRuntime', forbidden)
    writer = SimpleNamespace(write=lambda value: messages.append(json.loads(value)), drain=AsyncMock())
    messages = []
    factory = object()
    await relay_runtime.RuntimeTransportFactory.serve(factory, 'request', {'version': version, 'enrollment': 'opaque-test-token'}, object(), writer)
    assert selected == [(factory, 'opaque-test-token')]
    assert messages == [{'id': 'request', 'ok': True, 'result': {'version': version}}]
    State.close.assert_awaited_once()
