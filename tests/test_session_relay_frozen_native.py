"""Joined native config/auth refusal plus real supported-text final admission."""

import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.relay_native


def test_granted_frozen_native_security_and_final_main_admission(record_property):
    path=Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/installed_security_probe.py'
    spec=importlib.util.spec_from_file_location('frozen_native_probe',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    proof=module.capture_installed_security('claude',scenario='frozen_native')
    for key in ('productionFrozenMetadataPolicy','mutableSettingsScopesExcluded','strictEmptyMcpConfig',
        'mcpTamperingRefused','revokedNativePersonRefused','supportedNativeTextProcessed','finalMainAdmission',
        'realNativeReader','sameOwnerAfterRefusal','productionEmptyTreeStopped'):
        assert proof[key] is True,key
    assert len(proof['grantedNativeConfigAuthRefused'])==5
    assert proof['fixtureCapabilityOverrides'] and proof['terminalMediated'] is False
    assert proof['declaredCapabilities']=={}
    record_property('granted_frozen_native_security',json.dumps(proof,sort_keys=True))
