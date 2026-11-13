"""Pinned genuine Claude input and receipt-gated empty-boundary round trip."""

import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.relay_native


def test_repeated_native_activation_uses_fresh_grant_after_owned_dead_view_retirement(record_property):
    path=Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/installed_security_probe.py'
    spec=importlib.util.spec_from_file_location('roundtrip_probe',path)
    probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
    proof=probe.capture_installed_security('claude',scenario='repeat_activation')
    for key in ('actualPostInputTuiToSDK','genuineEmptyDrain','oldTuiWholeTreeExited','sameNativeIdAcrossRoundTrip',
            'sdkResumeUsesExactId','postHandoverSDKTurnAccepted','secondNativeGrantFresh','oldGrantAndHostNeverRevived','freshNativeInputProcessed','noReplay','productionEmptyTreeStopped'):
        assert proof[key] is True,key
    assert proof['modelCalls']==4
    record_property('native_repeat_activation',json.dumps(proof,sort_keys=True))
