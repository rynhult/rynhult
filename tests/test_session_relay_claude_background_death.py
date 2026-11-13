"""Native active SDK/Bash tree dies with its OS broker; unknown outcomes survive."""

import pytest
from test_session_relay_claude_process_death import process_death
from test_session_relay_runtime import runtime_case  # noqa: F401

pytestmark = pytest.mark.relay_native


async def test_active_sdk_background_tree_is_reaped_on_broker_sigkill(runtime_case, monkeypatch, record_property):  # noqa: F811
    await process_death(runtime_case, monkeypatch, record_property, background=True)
