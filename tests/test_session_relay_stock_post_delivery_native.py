import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.relay_native


@pytest.mark.parametrize('mode',['chat','expiry','cancel','revoke'])
def test_real_stock_key_decoded_before_same_question_lifecycle_change(record_property,mode):
    path=Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/installed_security_probe.py'
    spec=importlib.util.spec_from_file_location('stock_post',path)
    probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
    proof=probe.capture_installed_security('codex',scenario='stock_post_'+mode)
    assert proof['realKeyDecodedBeforeLifecycleChange'] and proof['decodedReplyRefused'] and proof['inputRemainsFenced']
    record_property('stock_post_delivery',json.dumps(proof,sort_keys=True))
