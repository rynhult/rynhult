
import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.relay_native


def test_native_file_request_inventory_never_answers_or_applies(record_property):
    path=Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/installed_security_probe.py'
    spec=importlib.util.spec_from_file_location('file_inventory_probe',path)
    probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
    proof=probe.capture_installed_security('codex',scenario='codex_file_inventory')
    assert proof['productionOwnerStopped'] and proof['noAnswerOrApplication']
    record_property('native_file_inventory',json.dumps(proof,sort_keys=True))
