"""Native request/item lifecycle inventory including interrupted questions."""
import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.relay_native


@pytest.mark.parametrize('scenario',['codex_ack','codex_ack_interrupt','codex_ack_accept','codex_ack_lost_before','codex_ack_lost_after','codex_ack_identical'])
def test_native_command_answer_lifecycle_inventory(scenario,record_property):
    path=Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/installed_security_probe.py'
    spec=importlib.util.spec_from_file_location('codex_ack_probe',path)
    probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
    proof=probe.capture_installed_security('codex',scenario=scenario)
    assert proof['productionOwnerStopped']
    assert proof['nativeRequest']['threadId']==proof['nativeId']
    assert proof['publicCookieApplicationOutcomeVisible']
    assert proof['applicationConfirmed']==(scenario not in ('codex_ack_interrupt','codex_ack_lost_before'))
    if scenario=='codex_ack_identical':assert proof['independentChosenReceipts']==2 and proof['responseWrites']==2
    record_property('native_codex_answer_lifecycle',json.dumps(proof,sort_keys=True))
