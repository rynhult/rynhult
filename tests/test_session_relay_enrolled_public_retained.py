"""Real native retained client read by a real authenticated public observer."""

import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.relay_native


def test_native_retained_client_public_read_has_no_input_authority_and_rechecks_cookie(record_property):
    path=Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/installed_security_probe.py'
    spec=importlib.util.spec_from_file_location('public_retained_probe',path)
    probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
    proof=probe.capture_installed_security('claude',scenario='retained_public')
    for key in ('nativeRetainedClientPublicCookieRead','retainedReadNeverClaimsInput',
            'revokedCookieCannotReadRetained','retainedReadExportsNoGrantOrProof',
            'nonemptyNativeDraftRetainedReadonly','newWriterFreshInput','noReplay','productionEmptyTreeStopped'):
        assert proof[key] is True,key
    assert proof['modelCalls']==3
    record_property('native_public_retained',json.dumps(proof,sort_keys=True))
