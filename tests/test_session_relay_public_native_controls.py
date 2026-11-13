"""Joined actual cookie public transport / native control refusal and continuation."""

import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.relay_native


def test_public_cookie_native_controls_refuse_before_effects_and_text_continues(record_property):
    path=Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/installed_security_probe.py'
    spec=importlib.util.spec_from_file_location('public_native_probe',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    proof=module.capture_installed_security('claude',scenario='public_controls')
    for key in ('realCookieCsrfAuthentication','publicGrantClaim','publicNativeTextProcessed','revokedCookieRefused',
        'noSideEffectsBeforeSupportedText','csrfRefusedBeforeInput','productionEmptyTreeStopped'):
        assert proof[key] is True,key
    assert len(proof['publicNativeControlRefusals'])==10 and proof['publicUnsupportedSchemaRefusals']==8
    record_property('public_native_controls',json.dumps(proof,sort_keys=True))
