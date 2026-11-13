import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.relay_native


@pytest.mark.parametrize('stage',['prepared','sent','completion','lost_ack','loss_before','loss_after','revoke_before','revoke_after','interrupt','read_loss_before','read_loss_after','edit_loss_before','edit_loss_after','read_failed_loss_after','edit_failed_loss_after','interrupt_recover_new','policy_before','policy_after','interrupt_before','strict_before','strict_after','ownership_before','ownership_after','account_before','account_after','workspace_before','workspace_after','loss_before_recover_new','loss_after_recover_new'])
def test_actual_execution_broker_crash_original_native_answer(record_property,stage):
    path=Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/installed_security_probe.py'
    spec=importlib.util.spec_from_file_location('execution_native',path)
    probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
    proof=probe.capture_installed_security('claude',scenario='tui_allow_execution_'+stage)
    assert proof['executionCrash']['executionBrokerAndMainReallyKilled']
    if stage in ('loss_before_recover_new','loss_after_recover_new'):
        fault=proof['executionCrash']['hungSupervisorFault']
        assert fault['nativeChildAliveDuringFreshRecovery'] and fault['brokerDeathFencePreserved']
        assert fault['resumedSupervisorEmptyTree'] and fault['nativePid'] != fault['supervisorPid']
    record_property('execution_crash',json.dumps(proof,sort_keys=True))
