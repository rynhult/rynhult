"""Native SDK expiry at its real five-minute deadline; no shortened-clock proof."""

import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.relay_native


def test_real_sdk_question_expires_at_original_deadline_and_late_channels_cannot_win(record_property):
    path=Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/installed_security_probe.py'
    spec=importlib.util.spec_from_file_location('natural_expiry_probe',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    result=module.capture_installed_security('claude',scenario='decision_expiry')
    for key in ('nativeSdkCallback','nativeConsumedToolDenialAtExpiry','durableDecisionExpired',
        'winnerAbsent','oneDecisionAuthority','nativeHookDecisionAbsent','lateOperationsNeverClaimed',
        'repeatNeverReplayed','toolNeverExecuted','productionEmptyTreeStopped'):
        assert result[key] is True,key
    assert result['nativeDeadlineSeconds']==300 and result['actualWaitSeconds']>=295
    assert result['nativeDenialDelaySeconds']>=295
    assert result['clockPatched'] is False and result['deadlineShortened'] is False
    assert result['lateAnswerChannelsRefused']==['chat','terminal']
    assert result['modelCalls']==2 and result['metadataReadForAdmission'] is False
    assert result['declaredCapabilities']=={} and result['terminalMediated'] is False
    record_property('natural_native_decision_expiry',json.dumps(result,sort_keys=True))
