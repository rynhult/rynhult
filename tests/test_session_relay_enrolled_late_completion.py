
import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.relay_native


def test_native_completion_first_arrives_after_observer_retirement(record_property):
    path=Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/installed_security_probe.py'
    spec=importlib.util.spec_from_file_location('late_completion',path)
    probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
    proof=probe.capture_installed_security('claude',scenario='tui_allow_late_completion')
    assert proof['lateCompletion']['noCachedReferenceBeforeReconciliation']
    record_property('late_completion',json.dumps(proof,sort_keys=True))
