import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.relay_native


@pytest.mark.parametrize('tool',['glob','grep'])
def test_unoffered_native_sdk_family_refuses_without_a_question(tool,record_property):
    path=Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/installed_security_probe.py'
    spec=importlib.util.spec_from_file_location('unoffered_probe',path)
    probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
    proof=probe.capture_installed_security('claude',scenario='claude_unoffered_'+tool)
    assert proof['noNativeQuestion'] and proof['unknownCallbackRefused'] and proof['productionEmptyTreeStopped']
    record_property('native_sdk_unoffered_tool',json.dumps(proof,sort_keys=True))
