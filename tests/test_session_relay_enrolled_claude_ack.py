"""Native SDK lifecycle inventory, including unanswered/corrupt delivery cases."""
import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.relay_native


@pytest.mark.parametrize('scenario',['claude_ack','claude_ack_allow','claude_ack_interrupt','claude_ack_lost_before','claude_ack_lost_after'])
def test_native_sdk_chosen_answer_lifecycle(scenario,record_property):
    path=Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/installed_security_probe.py'
    spec=importlib.util.spec_from_file_location('claude_ack_probe',path)
    probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
    proof=probe.capture_installed_security('claude',scenario=scenario)
    assert proof['productionEmptyTreeStopped']
    assert proof['requestId'] and proof['toolId']
    assert proof['applicationConfirmed']==(scenario not in ('claude_ack_interrupt','claude_ack_lost_before'))
    if scenario in ('claude_ack_interrupt','claude_ack_lost_before'):
        assert all(proof['publicInterrupt'][key] for key in ('cookieCsrfRouter','exactTurn','sameSubmittingAttachment','receiptDigestBound'))
    record_property('native_claude_ack',json.dumps(proof,sort_keys=True))
