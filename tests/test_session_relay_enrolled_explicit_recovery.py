"""Actual native failed target cleanup then public explicit same-id recovery."""
import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.relay_native


@pytest.mark.parametrize('scenario',['explicit_recovery','explicit_recovery_pre','explicit_recovery_sdk','explicit_recovery_sdk_connect','explicit_recovery_terminal','explicit_recovery_lost_owned'])
def test_native_public_explicit_failed_target_recovery(scenario,record_property):
    path=Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/installed_security_probe.py'
    spec=importlib.util.spec_from_file_location('recovery_probe',path)
    probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
    proof=probe.capture_installed_security('claude',scenario=scenario)
    for key in ('publicExplicitRecovery','oldAndPartialTreesVerifiedBeforeRecovery','recoveryNewFactoryAndController',
            'recoveryStableConversationLease','recoverySameIdResume','recoveryNoAutomaticQuery','recoveryOriginalReceiptAccepted',
            'recoveryOldConnectionStale','recoveryFreshPublicTurn','productionEmptyTreeStopped'):
        assert proof[key],key
    assert proof['lostOwnedAckRequiresNewReview']==(scenario=='explicit_recovery_lost_owned')
    assert proof['explicitRecoveryAttempts']==(2 if scenario=='explicit_recovery_lost_owned' else 1)
    record_property('native_explicit_recovery',json.dumps(proof,sort_keys=True))
