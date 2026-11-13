"""Native decision expiry/revision/current authority after real main verification."""
import pytest
from test_session_relay_claude_native_placement import (
    test_production_claude_owned_sdk_tmux_sdk_native_input as run_native,
)
from test_session_relay_runtime import runtime_case  # noqa: F401

pytestmark = pytest.mark.relay_native


@pytest.mark.parametrize('profile', ['sdk', 'tui'])
@pytest.mark.parametrize('fault', ['expiry', 'revision', 'policy', 'epoch', 'stop', 'account', 'positive'])
async def test_native_decision_scope_changes_after_verifier(runtime_case, monkeypatch, record_property, profile, fault):  # noqa: F811
    runtime_case.native_decision_fault = fault
    if profile == 'sdk':
        runtime_case.native_decision_verify_index = 3
    await run_native(runtime_case, monkeypatch, record_property,
        'http-sdk-allow-revoke' if profile == 'sdk' else 'http-permission')


@pytest.mark.parametrize('fault', ['expiry', 'revision', 'policy', 'epoch', 'stop', 'account'])
async def test_native_codex_decision_scope_changes_after_verifier(runtime_case, monkeypatch, record_property, fault):  # noqa: F811
    from test_session_relay_codex_native_placement import (
        test_production_codex_native_public_take_and_input as run_codex,
    )
    runtime_case.native_decision_fault = fault
    await run_codex(runtime_case, monkeypatch, record_property, False, False, True)


@pytest.mark.parametrize('profile', ['sdk', 'tui'])
@pytest.mark.parametrize('fault', ['expiry', 'revision', 'positive'])
async def test_native_deny_scope_changes_after_verifier(runtime_case, monkeypatch, record_property, profile, fault):  # noqa: F811
    runtime_case.native_decision_fault = fault
    runtime_case.native_decision_effect = 'deny'
    if profile == 'sdk':
        runtime_case.native_decision_verify_index = 3
    await run_native(runtime_case, monkeypatch, record_property,
        'http-sdk-allow-revoke' if profile == 'sdk' else 'http-permission')


@pytest.mark.parametrize('profile', ['sdk', 'tui', 'codex'])
async def test_native_saved_budget_changes_after_verifier(runtime_case, monkeypatch, record_property, profile):  # noqa: F811
    runtime_case.native_decision_fault = 'budget'
    if profile == 'codex':
        from test_session_relay_codex_native_placement import (
            test_production_codex_native_public_take_and_input as run_codex,
        )
        await run_codex(runtime_case, monkeypatch, record_property, False, False, True)
    else:
        runtime_case.native_decision_verify_index = 3 if profile == 'sdk' else 1
        await run_native(runtime_case, monkeypatch, record_property,
            'http-sdk-allow-revoke' if profile == 'sdk' else 'http-permission')


@pytest.mark.parametrize('fault', ['expiry', 'revision'])
async def test_native_codex_queued_decision_scope(runtime_case, monkeypatch, record_property, fault):  # noqa: F811
    from test_session_relay_codex_native_placement import (
        test_production_codex_native_public_take_and_input as run_codex,
    )
    runtime_case.native_decision_fault = fault
    runtime_case.native_queue_decision = True
    await run_codex(runtime_case, monkeypatch, record_property, False, False, True)
