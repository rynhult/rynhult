"""Pinned SDK non-Bash chosen outcomes and exact loss/cancellation cases."""
import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.relay_native


@pytest.mark.parametrize('tool',['read','write','edit'])
@pytest.mark.parametrize('mode',['allow','deny','interrupt','lost_before','lost_after'])
def test_nonbash_native_chosen_outcome(tool,mode,record_property):
    path=Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/installed_security_probe.py'
    spec=importlib.util.spec_from_file_location('tool_ack_probe',path)
    probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
    proof=probe.capture_installed_security('claude',scenario='claude_ack_'+tool+'_'+mode)
    assert proof['productionEmptyTreeStopped'] and proof['tool'].lower()==tool
    assert proof['applicationConfirmed']==(mode not in ('interrupt','lost_before'))
    if mode in ('interrupt','lost_before'):
        assert all(proof['publicInterrupt'][key] for key in ('cookieCsrfRouter','exactTurn','sameSubmittingAttachment','receiptDigestBound'))
    record_property('native_sdk_tool_answer',json.dumps(proof,sort_keys=True))
