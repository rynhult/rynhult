
import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.relay_native


def test_native_overwrite_notice_never_answers_and_person_terminal_continues(record_property):
    path=Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/installed_security_probe.py'
    spec=importlib.util.spec_from_file_location('native_prompt',path)
    probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
    proof=probe.capture_installed_security('claude',scenario='tui_native_prompt')
    assert proof['nativeOverwriteDetected'] and proof['noPermissionRequestCard'] and proof['remoteAllowRefused']
    assert proof['productModalKeysSent']==0 and proof['personAnswerContinued'] and proof['noteResolved']
    record_property('native_prompt_handoff',json.dumps(proof,sort_keys=True))
