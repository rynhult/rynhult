"""Real SDK failed start remains fenced and verifies its own empty tree."""
import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.relay_native


@pytest.mark.parametrize('scenario',['failed_sdk_target','failed_sdk_connect'])
def test_native_sdk_post_connect_refusal_reaps_exact_target_without_retry(scenario,record_property):
    path=Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/installed_security_probe.py'
    spec=importlib.util.spec_from_file_location('failed_sdk_probe',path)
    probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
    proof=probe.capture_installed_security('claude',scenario=scenario)
    for key in ('failedSDKIdentityPinnedBeforeRefusal','sourceOwnWholeTreeExited',
            'failedSDKOwnWholeTreeExited','failedSDKNeverOwned','failedSDKMainRecoveryRequired','failedSDKNoReplay','productionEmptyTreeStopped'):
        assert proof[key] is True,key
    assert proof['modelCalls']==2
    assert proof['nativeSDKConnectError']==(scenario=='failed_sdk_connect')
    assert proof['nativeSDKPostConnectAuthorityLoss']==(scenario=='failed_sdk_target')
    record_property('native_failed_sdk_target',json.dumps(proof,sort_keys=True))
