"""Conclusive kernel retirement is independent of a failed final publication."""
from types import SimpleNamespace

from acc.broker.claude_owner import ClaudeOwnerFactory
from acc.session_relay.adapters.claude_agent import ClaudeAgentAdapter


async def test_kernel_exit_allows_cleanup_but_publication_failure_still_refuses_idle():
    adapter = ClaudeAgentAdapter(policy=lambda *_: True, publish=None)
    adapter.process = SimpleNamespace(returncode=0)
    adapter.exit_verified = adapter.failure = True
    adapter.spec = SimpleNamespace(binding=SimpleNamespace(launch_policy_digest='fixture'))
    factory = object.__new__(ClaudeOwnerFactory)
    factory.current, factory.exit_qualified = adapter, True
    factory.enclosure = SimpleNamespace(proof=lambda *_: True)  # explicit synthetic kernel receipt
    adapter.attestation = SimpleNamespace(process_identity='fixture-process', native_conversation='fixture-native')
    factory.close_complete = False  # bypassed constructor; cleanup has not run
    assert await factory.exited()
    assert not (await adapter.prepare_switch()).idle
    adapter.exit_verified = False
    assert not await factory.exited()
