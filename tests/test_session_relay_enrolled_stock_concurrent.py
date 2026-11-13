import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.relay_native


@pytest.mark.parametrize('mode',['native_first','chat_first','revoked_native'])
def test_pinned_identical_concurrent_stock_questions_keep_exact_actor_and_operation(record_property,mode):
    path=Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/installed_security_probe.py'
    spec=importlib.util.spec_from_file_location('stock_concurrent',path)
    probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
    proof=probe.capture_installed_security('codex',scenario='stock_concurrent_'+mode)
    assert proof['identicalCommands'] and proof['originalFrameActorPreserved'] and proof['noBorrowedGrant']
    assert len(set(proof['nativeRequestIds']))==2 and len(proof['providerAnswers'])==2
    assert all(receipt['state']=='accepted' for receipt in proof['receipts']) and proof['noReplay']
    assert proof['nativeProcessed']==proof['mainProcessed']==(2 if mode=='revoked_native' else 3)
    record_property('native_stock_concurrent',json.dumps(proof,sort_keys=True))
