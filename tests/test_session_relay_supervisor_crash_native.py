
import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.relay_native


def test_supervisor_kernel_exit_fences_live_vendor_and_recovers_without_adoption(record_property):
    path=Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/installed_security_probe.py'
    spec=importlib.util.spec_from_file_location('supervisor_crash',path)
    probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
    proof=probe.capture_installed_security('codex',scenario='stock_race_supervisor_recover_new')
    assert proof['supervisorKernelKilled'] and proof['vendorAliveAtKernelExit'] and proof['fencedSeconds']<1
    assert proof['mutationValidationRefusedBeforePipeEof'] and proof['nativeMutationsAfterFence']==0
    assert proof['oldCleanupUnknown'] and proof['emptyTreeReceiptAbsent'] and proof['noAdoptionOrReplay']
    assert proof['receipt']['state']=='uncertain' and proof['decision']['status']=='outcome_unknown'
    assert proof['nativeProcessed']==2 and len(proof['providerResponseWrites'])==1
    fresh=proof['publicFreshSessionRecovery']
    assert fresh['newProvider']=='codex' and fresh['newSessionId']!=fresh['oldSessionId']
    assert fresh['oldSnapshotEventsReceiptsDecisionsUnchanged'] and fresh['freshNativeEmptyTreeProven']
    record_property('supervisor_crash',json.dumps(proof,sort_keys=True))
