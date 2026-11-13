import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.relay_native


@pytest.mark.parametrize('mode',['ack','decline','interrupt','lost_before','lost_after'])
def test_native_file_approval_original_outcome(mode,record_property):
    path=Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/installed_security_probe.py'
    spec=importlib.util.spec_from_file_location('file_ack_probe',path)
    probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
    proof=probe.capture_installed_security('codex',scenario='codex_file_'+mode)
    assert proof['productionOwnerStopped']
    assert proof['applicationConfirmed']==(mode not in ('interrupt','lost_before'))
    record_property('native_file_answer',json.dumps(proof,sort_keys=True))
