"""Actual native permissions plus Hub CAS; public TUI and reconciliation stay gated."""

import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.relay_native


def test_enrolled_native_permission_has_one_hub_winner_and_never_replays_unknown(record_property):
    path=Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/installed_security_probe.py'
    spec=importlib.util.spec_from_file_location('decision_probe',path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result=module.capture_installed_security('claude',scenario='decisions')
    assert result['realNativeOwner'] and result['productionHubCAS']
    assert set(result['invalidRefused'])=={'choice','digest','revision','expired_operation','revoked'}
    assert result['unknownPreserved'] and result['unknownNotReplayed']
    assert result['oneDecisionAuthority'] and result['nativePermissionHook'] is False
    assert result['productionEmptyTreeStopped'] and result['modelCalls']==2
    assert result['declaredCapabilities']=={} and result['terminalMediated'] is False
    record_property('enrolled_native_decisions',json.dumps(result,sort_keys=True))
