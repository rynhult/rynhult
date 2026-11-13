"""Actual native pending-tool and installed-helper exits; isolated API-key only."""
import pytest
from test_session_relay_claude_native_placement import (
    test_production_claude_owned_sdk_tmux_sdk_native_input as run_native,
)
from test_session_relay_runtime import runtime_case  # noqa: F401

pytestmark = pytest.mark.relay_native


@pytest.mark.parametrize('profile', ['sdk', 'tui'])
@pytest.mark.parametrize('fault', ['vendor', 'guard-helper'])
async def test_native_pending_or_guard_helper_exit(runtime_case, monkeypatch, record_property, profile, fault):  # noqa: F811
    runtime_case.native_pending_exit = fault == 'vendor'
    runtime_case.native_guard_exit = fault == 'guard-helper'
    await run_native(runtime_case, monkeypatch, record_property,
        'http-sdk-allow-revoke' if profile == 'sdk' else 'http-permission')


async def test_native_permission_hook_process_loss(runtime_case, monkeypatch, record_property):  # noqa: F811
    runtime_case.native_hook_exit = True
    await run_native(runtime_case, monkeypatch, record_property, 'http-permission')
