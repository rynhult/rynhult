"""Malformed/stale typed metadata injected after actual native SDK decoding.

A real native person Deny is issued once; synthetic metadata must not settle its
application. This is not a claim of seventeen provider callback or filesystem trust.
"""
import pytest
from test_session_relay_claude_native_placement import (
    test_production_claude_owned_sdk_tmux_sdk_native_input as run_native,
)
from test_session_relay_runtime import runtime_case  # noqa: F401

pytestmark = pytest.mark.relay_native


@pytest.mark.parametrize('fault', ['result-session', 'result-failed', 'parent-tool', 'tool-reference', 'result-uuid', 'duplicate-result', None])
async def test_native_sdk_application_source_metadata(runtime_case, monkeypatch, record_property, fault):  # noqa: F811
    runtime_case.native_sdk_metadata_fault = fault
    runtime_case.native_decision_fault = 'positive'
    runtime_case.native_decision_effect = 'deny'
    runtime_case.native_decision_verify_index = 3
    await run_native(runtime_case, monkeypatch, record_property, 'http-sdk-allow-revoke')
