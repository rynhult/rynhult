"""Native failed replacements stay recovery fenced and prove their own cleanup."""
import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.relay_native


@pytest.mark.parametrize('scenario',['failed_target','failed_target_post'])
def test_failed_native_target_requires_explicit_recovery_and_separate_cleanup(scenario,record_property):
    path=Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/installed_security_probe.py'
    spec=importlib.util.spec_from_file_location('failed_target_probe',path)
    probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
    proof=probe.capture_installed_security('claude',scenario=scenario)
    for key in ('failedTargetSourceWholeTreeExited','failedTargetNeverOwned','failedTargetMainRecoveryRequired',
            'failedTargetFactoryReclaimed','failedTargetOldOwnerNotRevived','failedTargetNoReplay','productionEmptyTreeStopped'):
        assert proof[key] is True,key
    assert proof['modelCalls']==2
    assert proof['failedTargetOwnEmptyTreeProof']==(scenario=='failed_target_post')
    record_property('native_failed_target',json.dumps(proof,sort_keys=True))
