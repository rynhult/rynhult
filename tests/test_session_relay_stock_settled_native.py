import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.relay_native


@pytest.mark.parametrize('mode',['cancel_applied','chat_loser'])
def test_native_stock_original_winner_survives_late_cancellation_or_same_question_chat(record_property,mode):
    path=Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/installed_security_probe.py'
    spec=importlib.util.spec_from_file_location('stock_settled',path)
    probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
    proof=probe.capture_installed_security('codex',scenario='stock_race_'+mode)
    assert proof['receipt']['state']=='accepted' and proof['decision']['status']=='answered'
    assert proof['exactNativeApplicationPublished'] and proof['originalWinnerUnchanged'] and proof['noReplay']
    assert len(proof['providerResponseWrites'])==len(proof['responseAttempts'])==1
    assert proof['nativeProcessed']==proof['mainProcessed']==3
    record_property('stock_settled',json.dumps(proof,sort_keys=True))
