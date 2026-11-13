import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.relay_native


@pytest.mark.parametrize('loss',['before','after'])
def test_pinned_public_stock_cancel_response_loss_preserves_exact_outcome(record_property,loss):
    path=Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/installed_security_probe.py'
    spec=importlib.util.spec_from_file_location('stock_cancel_loss',path)
    probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
    proof=probe.capture_installed_security('codex',scenario='stock_cancel_lost_'+loss)
    assert proof['choice']=='cancel' and len(proof['nativeResponseAttempts'])==1 and proof['noReplay']
    assert proof['receipt']['state']==('uncertain' if loss=='before' else 'accepted')
    assert proof['nativeProcessedFrame']==proof['mainProcessedFrame']==(2 if loss=='before' else 3)
    record_property('native_stock_cancel_loss',json.dumps(proof,sort_keys=True))
