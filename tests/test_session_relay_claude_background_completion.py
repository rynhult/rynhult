"""Actual native Bash completion must drain the SDK task ledger without a new turn."""

import pytest
from test_session_relay_claude_churn import test_production_claude_owner_hook_churn as run_churn
from test_session_relay_runtime import runtime_case  # noqa: F401

pytestmark = pytest.mark.relay_native


async def test_native_background_completion_releases_ledger_preserves_unknown_decisions(runtime_case, monkeypatch, record_property):  # noqa: F811
    await run_churn(runtime_case, monkeypatch, record_property, cycles=0, background_mode='complete')
