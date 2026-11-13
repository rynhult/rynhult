"""Actual API-key Claude SDK receives an exact source-run/assignment-bound team turn."""
import pytest
from test_session_relay_claude_service_native import run_native_service
from test_session_relay_runtime import runtime_case  # noqa: F401

pytestmark = pytest.mark.relay_native


@pytest.mark.parametrize('revoke_source', [False, True], ids=['accepted', 'ended-source-run'])
async def test_native_claude_team_service_keeps_assignment_source_and_api_only(runtime_case, monkeypatch, record_property, revoke_source):  # noqa: F811
    await run_native_service(runtime_case, monkeypatch, record_property, team=True, revoke_source=revoke_source)
