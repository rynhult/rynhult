"""Bounded real native retained views and observer streams."""

import pytest
from test_session_relay_runtime import runtime_case  # noqa: F401

pytestmark = pytest.mark.relay_native


async def test_native_codex_retained_generations(runtime_case, monkeypatch, record_property):  # noqa: F811
    from test_session_relay_codex_native_placement import (
        test_production_codex_native_public_take_and_input as run_native,
    )
    runtime_case.native_generation_capacity = True
    await run_native(runtime_case, monkeypatch, record_property, False, False, False)


async def test_native_claude_observer_stream_capacity(runtime_case, monkeypatch, record_property):  # noqa: F811
    from test_session_relay_claude_native_placement import (
        test_production_claude_owned_sdk_tmux_sdk_native_input as run_native,
    )
    runtime_case.native_observer_capacity = True
    await run_native(runtime_case, monkeypatch, record_property, 'http-warm')
