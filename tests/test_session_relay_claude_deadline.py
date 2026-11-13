"""Explicit native fixture deadline diagnostic, not an owner-lifetime qualification."""
import pytest
from test_session_relay_claude_churn import test_production_claude_owner_hook_churn as run_churn
from test_session_relay_runtime import runtime_case  # noqa: F401

pytestmark = pytest.mark.relay_native


@pytest.mark.relay_long_native
async def test_native_deadline_preserves_capture_timeout(runtime_case, monkeypatch, record_property, caplog):  # noqa: F811
    with pytest.raises(Exception) as failed:
        await run_churn(runtime_case, monkeypatch, record_property, cycles=2, proof_budget=15)
    diagnostic = repr(failed.value) + '\n' + '\n'.join(getattr(failed.value, '__notes__', []))
    assert 'TimeoutExpired' in diagnostic, diagnostic
    assert not any('Task exception was never retrieved' in record.getMessage() for record in caplog.records)
    record_property('native_capture_timeout_retrieved', True)
    record_property('primary_failure', type(failed.value).__name__)
