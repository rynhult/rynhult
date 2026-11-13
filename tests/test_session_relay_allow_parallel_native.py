import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.relay_native


@pytest.mark.parametrize('mode',['parallel','ambiguous'])
def test_native_simultaneous_allow_questions(record_property,mode):
    path=Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/installed_security_probe.py'
    spec=importlib.util.spec_from_file_location('parallel_native',path)
    probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
    proof=probe.capture_installed_security('claude',scenario='tui_allow_'+mode)
    assert proof['twoActualNativePendingQuestions']
    record_property('parallel_allow',json.dumps(proof,sort_keys=True))
