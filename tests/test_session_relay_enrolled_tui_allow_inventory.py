
import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.relay_native


def test_native_allow_stderr_reference_inventory_does_not_promote_a_receipt(record_property):
    path=Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/installed_security_probe.py'
    spec=importlib.util.spec_from_file_location('tui_allow_inventory',path)
    probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
    proof=probe.capture_installed_security('claude',scenario='tui_allow_inventory')
    assert proof['inventoryOnly'] and proof['unknownPreserved']
    assert len(proof['privateInvocationReferences'])==1
    assert proof['hookNativeToolUseIds']==[None]
    record_property('native_tui_allow_inventory',json.dumps(proof,sort_keys=True))
