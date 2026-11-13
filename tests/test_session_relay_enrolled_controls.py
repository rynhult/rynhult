"""Actual retained owner control refusal; no public writer capability promotion."""

import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.relay_native


def test_enrolled_native_controls_cannot_mutate_or_restart_retained_owner(record_property):
    path=Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/installed_security_probe.py'
    spec=importlib.util.spec_from_file_location('controls_probe',path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result=module.capture_installed_security(scenario='controls')
    assert len(result['actualControlMatrix'])==20
    assert result['controlsRefusedBeforeModel'] and result['sameOwnerTextContinuation']
    assert result['productionOwnerStopped'] and result['modelCalls']==2
    assert result['publicWriterQualified'] is False
    assert result['declaredCapabilities']=={} and result['terminalMediated'] is False
    record_property('enrolled_native_controls',json.dumps(result,sort_keys=True))
