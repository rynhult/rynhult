"""Actual stock private app-server/remote TUI exit gates, offline only."""
import pytest
from test_session_relay_codex_native_placement import (
    test_production_codex_native_public_take_and_input as run_native,
)
from test_session_relay_runtime import runtime_case  # noqa: F401

pytestmark = pytest.mark.relay_native


@pytest.mark.parametrize('stage', ['idle', 'busy', 'pending'])
@pytest.mark.parametrize('kind', ['vendor', 'view', 'view-supervisor'])
async def test_native_codex_provider_exit(runtime_case, monkeypatch, record_property, stage, kind):  # noqa: F811
    runtime_case.native_exit_stage = stage
    runtime_case.native_provider_exit = kind
    await run_native(runtime_case, monkeypatch, record_property, False, False, stage == 'pending')
