"""Real pinned SDK raw identities survive its typed callback boundary."""

import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.relay_native


def test_owned_native_sdk_permission_binds_exact_wire_ids_without_settling_answer(record_property):
    path=Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/installed_security_probe.py'
    spec=importlib.util.spec_from_file_location('wire_probe',path)
    probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
    result=probe.capture_installed_security('claude',scenario='decision_wire')
    assert result['exactSDKRequestBinding'] and result['responseWriteIsNotAcknowledgement']
    assert result['unknownPreserved'] and result['unknownNotReplayed']
    assert result['productionEmptyTreeStopped']
    record_property('native_sdk_permission_wire',json.dumps(result,sort_keys=True))
