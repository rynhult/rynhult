import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.relay_native


@pytest.mark.parametrize('mode',['owner','owner_applied','transport_applied'])
def test_stock_failure_recovers_into_new_codex(record_property,mode):
    path=Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/installed_security_probe.py'
    spec=importlib.util.spec_from_file_location('fresh_codex',path)
    probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
    proof=probe.capture_installed_security('codex',scenario='stock_race_fresh_codex_'+mode+'_recover_new')
    assert proof['receipt']['state']==('uncertain' if mode=='owner' else 'accepted')
    fresh=proof['publicFreshSessionRecovery']
    assert proof['freshRecoveryProvider']==fresh['newProvider']=='codex'
    assert fresh['newSessionId']!=fresh['oldSessionId'] and fresh['newNativeId']!=fresh['oldNativeId']
    assert fresh['ordinaryPersonCookieCsrfLaunch'] and fresh['realReviewedReservationEnrollmentAndNativeAppServer']
    assert fresh['blankLaunchNoModelCall'] and fresh['freshModelCalls']==1 and fresh['explicitNewChatOnly']
    assert fresh['oldSnapshotEventsReceiptsDecisionsUnchanged'] and fresh['noOldPromptOrApprovalReplay']
    assert fresh['freshNativeOwnerStopped'] and fresh['freshNativeEmptyTreeProven']
    if mode!='owner':assert proof['durableApplicationBeforeLoss'] and proof['oldOwnerEmptyTreeProven'] and fresh['oldMessageOutputPresent']
    record_property('stock_fresh_codex',json.dumps(proof,sort_keys=True))
