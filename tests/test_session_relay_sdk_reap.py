"""A failed SDK close never authorizes signalling a replacement process."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from acc.broker.claude_owner import ClaudeOwnerFactory
from acc.session_relay.adapters.claude_agent import ClaudeAgentAdapter


@pytest.mark.parametrize('fault', ['no-start-proof', 'reused-pid', 'no-exit-proof', 'pidfd-denied'])
async def test_sdk_fallback_reap_requires_exact_identity_and_empty_tree(monkeypatch, fault):
    import os

    adapter = ClaudeAgentAdapter(policy=lambda *_: True, publish=AsyncMock())
    adapter.attestation = SimpleNamespace(run_id='fixture-run', process_identity='987654:old-incarnation')
    adapter.process = SimpleNamespace(returncode=-9 if fault == 'no-exit-proof' else None, wait=AsyncMock(return_value=-9))
    factory = object.__new__(ClaudeOwnerFactory)
    factory.current = adapter
    factory.enclosures = {'fixture-run': SimpleNamespace(proof=lambda name, _: name == 'started.json' and fault != 'no-start-proof')}
    signal = Mock(side_effect=AssertionError('No signal may cross an unproven process identity'))
    monkeypatch.setattr('acc.broker.claude_owner.signal.pidfd_send_signal', signal)
    monkeypatch.setattr('acc.broker.relay_enclosure.incarnation', lambda _: 'replacement-incarnation')
    if fault == 'reused-pid':
        monkeypatch.setattr('acc.broker.claude_owner.os.pidfd_open', lambda _: os.open('/dev/null', os.O_RDONLY))
    else:
        monkeypatch.setattr('acc.broker.claude_owner.os.pidfd_open', Mock(side_effect=PermissionError('Fixture refuses process access')))
    assert await factory.reap_sdk_owner() is False
    assert adapter.exit_verified is False
    signal.assert_not_called()
