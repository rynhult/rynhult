"""Actual owned native profiles plus real main history reset/compaction."""
import pytest
from test_session_relay_runtime import runtime_case  # noqa: F401

pytestmark = pytest.mark.relay_native


@pytest.mark.parametrize('provider', ['sdk', 'codex'])
@pytest.mark.parametrize('fault', ['epoch', 'compaction'])
async def test_native_history_reset_and_compaction(runtime_case, monkeypatch, record_property, provider, fault):  # noqa: F811
    runtime_case.native_history_fault = fault
    if provider == 'codex':
        from test_session_relay_codex_native_placement import (
            test_production_codex_native_public_take_and_input as run_native,
        )
        await run_native(runtime_case, monkeypatch, record_property, False, False, False)
    else:
        from test_session_relay_claude_native_placement import (
            test_production_claude_owned_sdk_tmux_sdk_native_input as run_native,
        )
        await run_native(runtime_case, monkeypatch, record_property, 'http-warm')
