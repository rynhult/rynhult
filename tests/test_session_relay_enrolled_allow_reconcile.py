
import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.relay_native


def test_pinned_tui_allow_reconciles_original_after_real_observer_deadline(record_property):
    path=Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/installed_security_probe.py'
    spec=importlib.util.spec_from_file_location('allow_reconcile',path)
    probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
    proof=probe.capture_installed_security('claude',scenario='tui_allow_reconcile')
    assert proof['durableAllow']['realObserverElapsedSeconds']>=30
    record_property('durable_tui_allow',json.dumps(proof,sort_keys=True))
