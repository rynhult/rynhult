"""Failed cleanup cannot certify an unknown native tree or release its identity."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from acc.broker.claude_owner import ClaudeOwnerFactory
from acc.session_relay import protocol as p


@pytest.mark.parametrize('cancelled', [False, True])
async def test_unknown_tree_keeps_proof_handles_and_lease_until_verified_exit(cancelled):
    process = SimpleNamespace(returncode=0, tree_exited=AsyncMock(return_value=False))
    owner = SimpleNamespace(process=process, close=AsyncMock(
        side_effect=asyncio.CancelledError if cancelled else None))
    proof_process, enclosure = SimpleNamespace(close=Mock()), SimpleNamespace(close=Mock())
    factory = object.__new__(ClaudeOwnerFactory)
    factory.close_complete = factory.closed = False
    factory.launch_attempt = None
    factory.current, factory.exit_qualified = owner, True
    factory.input_attachment = SimpleNamespace(close=AsyncMock())
    factory.observer = SimpleNamespace(close=AsyncMock())
    factory.hooks = SimpleNamespace(close=AsyncMock())
    factory.processes, factory.enclosures = [proof_process], {'old-run': enclosure}
    factory.lease = SimpleNamespace(stop=AsyncMock())
    with pytest.raises(asyncio.CancelledError if cancelled else p.RelayError):
        await factory.close()
    assert factory.closed and not factory.close_complete
    assert not await factory.exited()
    proof_process.close.assert_not_called()
    enclosure.close.assert_not_called()
    factory.lease.stop.assert_not_awaited()
    for transport in (factory.input_attachment, factory.observer, factory.hooks):
        transport.close.assert_awaited_once()

    # A later conclusive empty-tree proof can reclaim the same quarantined
    # resources; no replacement owner or borrowed authority is manufactured.
    owner.close.side_effect = None
    process.tree_exited.return_value = True
    await factory.close()
    assert factory.close_complete and await factory.exited()
    proof_process.close.assert_called_once()
    enclosure.close.assert_called_once()
    factory.lease.stop.assert_awaited_once()
