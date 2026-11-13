"""Pinned native failed-command lifecycle, including original chosen receipt."""
import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.relay_native


@pytest.mark.parametrize('scenario',['codex_ack_failed','codex_ack_failed_lost'])
def test_failed_command_answer_lifecycle(scenario,record_property):
    path=Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/installed_security_probe.py'
    spec=importlib.util.spec_from_file_location('failed_ack_probe',path)
    probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
    proof=probe.capture_installed_security('codex',scenario=scenario)
    assert proof['productionOwnerStopped'] and proof['applicationConfirmed']
    assert all(r['state']=='accepted' for r in proof['finalReceipts'])
    assert any(e.get('status')=='failed' and e.get('exitCode')==7 for e in proof['lifecycle'])
    assert proof['responseAttempts']==1 and proof['responseWrites']==1
    record_property('native_failed_command_answer',json.dumps(proof,sort_keys=True))
