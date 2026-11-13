"""Real native TUI absolute expiry and main continuation, no shortened clock."""

import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.relay_native


def test_real_tui_permission_expires_and_late_choices_never_execute(record_property):
    path=Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/installed_security_probe.py'
    spec=importlib.util.spec_from_file_location('tui_expiry_probe',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    proof=module.capture_installed_security('claude',scenario='tui_expiry')
    for key in ('nativeTuiPermissionHook','nativeQuestionExpired','decisionWinnerAbsent','lateChoicesRefusedWithoutDispatch',
        'noReplay','toolNeverExecuted','followupNativeInputProcessed','productionEmptyTreeStopped'):
        assert proof[key] is True,key
    assert proof['nativeDeadlineSeconds']==15 and proof['nativeDenialSeconds']>=13
    assert proof['clockPatched'] is False and proof['deadlineShortened'] is False
    record_property('native_tui_decision_expiry',json.dumps(proof,sort_keys=True))
