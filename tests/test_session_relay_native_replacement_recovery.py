"""Original durable fact recovery while a separate person-enabled enrollment lives.

This qualifies new public session enrollment on the same account, not same-public-
session re-enrollment after crash (no such new-session path is manufactured here).
"""
import pytest
from test_session_relay_application_os_restart import (
    test_native_unpublished_application_survives_os_restart as run_native,
)
from test_session_relay_node_runtime import node_runtime  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401

pytestmark = pytest.mark.relay_native


@pytest.mark.parametrize('allow', [False, True])
@pytest.mark.parametrize('fault', ['fresh', 'grant', 'publication', 'hooks'])
async def test_original_fact_with_new_live_enrollment(node_runtime, allow, fault, record_property):  # noqa: F811
    node_runtime.native_replacement_fault = fault
    await run_native(node_runtime, 'both', allow, record_property)
