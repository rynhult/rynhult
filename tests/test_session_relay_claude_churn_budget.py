"""Serial diagnostic of the original deadline; never a default-suite job."""
import pytest
from test_session_relay_claude_churn import test_production_claude_owner_hook_churn as run_churn
from test_session_relay_runtime import runtime_case  # noqa: F401

pytestmark = pytest.mark.relay_native


@pytest.mark.relay_long_native
async def test_claude_131_serial_original_deadline(runtime_case, monkeypatch, record_property):  # noqa: F811
    await run_churn(runtime_case, monkeypatch, record_property, cycles=65, proof_budget=1200)
