import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.relay_native


@pytest.mark.parametrize('mode',['failed','failed_lost','failed_before','cancel_refused'])
def test_native_file_remaining_outcomes(mode,record_property):
    path=Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/installed_security_probe.py'
    spec=importlib.util.spec_from_file_location('remaining_file_probe',path)
    probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
    proof=probe.capture_installed_security('codex',scenario='codex_file_'+mode)
    assert proof['productionOwnerStopped']
    assert proof['applicationConfirmed']==(mode!='failed_before')
    if mode=='cancel_refused':assert proof['invalidCancelRefused'] and proof['responseWrites']==1
    record_property('native_file_remaining',json.dumps(proof,sort_keys=True))
