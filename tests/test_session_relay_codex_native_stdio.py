"""Real private app-server stdin/stdout fault matrix; synthetic offline model."""
import pytest
from test_session_relay_codex_native_placement import (
    test_production_codex_native_public_take_and_input as run_native,
)
from test_session_relay_runtime import runtime_case  # noqa: F401

pytestmark = pytest.mark.relay_native


@pytest.mark.parametrize('fault', ['write-prefix', 'write-partial', 'read-eof', 'read-partial', 'lost-response'])
async def test_native_codex_stdio_fault(runtime_case, monkeypatch, record_property, fault):  # noqa: F811
    runtime_case.native_stdio_fault = fault
    await run_native(runtime_case, monkeypatch, record_property, False, False, False)
