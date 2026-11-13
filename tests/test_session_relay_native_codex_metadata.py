"""Real private app-server with source metadata faults before RPC decoding."""
import pytest
from test_session_relay_codex_native_placement import (
    test_production_codex_native_public_take_and_input as run_native,
)
from test_session_relay_runtime import runtime_case  # noqa: F401

pytestmark = pytest.mark.relay_native


@pytest.mark.parametrize('fault', ['thread', 'turn', 'item', 'environment', 'started', 'extra'])
async def test_native_codex_source_metadata(runtime_case, monkeypatch, record_property, fault):  # noqa: F811
    runtime_case.native_metadata_fault = fault
    await run_native(runtime_case, monkeypatch, record_property, False, False, True)
