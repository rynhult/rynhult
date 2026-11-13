import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.relay_native


@pytest.mark.parametrize('profile',['failed','lost_before','lost_after'])
def test_pinned_public_stock_command_failure_and_response_loss_never_replay(record_property,profile):
    path=Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/installed_security_probe.py'
    spec=importlib.util.spec_from_file_location('stock_outcomes',path)
    probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
    proof=probe.capture_installed_security('codex',scenario='stock_public_'+profile)
    assert proof['publicCookieCsrf'] and proof['originalFrameActor'] and proof['noReplay']
    assert len(proof['nativeResponseAttempts'])==1
    if profile=='lost_before':
        assert proof['receipt']['state']=='uncertain' and proof['unknownPreserved'] and proof['responseWrites']==0
        assert proof['nativeProcessedFrame']==proof['mainProcessedFrame']==2
    else:
        assert proof['receipt']['state']=='accepted' and proof['conclusiveProviderApplication'] and proof['responseWrites']==1
        assert proof['nativeProcessedFrame']==proof['mainProcessedFrame']==3
        if profile=='failed':assert proof['commandEvidence']['exitCode']==7 and proof['commandEvidence']['status']=='failed'
    record_property('native_stock_outcome',json.dumps(proof,sort_keys=True))
