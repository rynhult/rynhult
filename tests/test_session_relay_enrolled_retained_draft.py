"""Native unsent old client survives read-only; fresh input is never replayed."""

import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.relay_native


def test_nonempty_old_claude_client_retains_draft_readonly_and_new_native_input_is_fresh(record_property):
    path=Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/installed_security_probe.py'
    spec=importlib.util.spec_from_file_location('retained_draft_probe',path)
    probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
    proof=probe.capture_installed_security('claude',scenario='retained_draft')
    for key in ('nonemptyNativeDraftRetainedReadonly','oldDraftDisplayedNatively','oldWatermarksNeverAdvanced',
            'newWriterFreshInput','oldClientNeverRevived','nativeSameIdAcrossRetainedDraft',
            'exactFreshNativePromptAccepted','noReplay','productionEmptyTreeStopped'):
        assert proof[key] is True,key
    assert proof['modelCalls']==3
    record_property('native_retained_draft',json.dumps(proof,sort_keys=True))
