import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.relay_native


@pytest.mark.parametrize('mode',['chat','expiry','revoke'])
def test_pinned_public_stock_lifecycle_retires_exact_alias_without_key_replay(record_property,mode):
    path=Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/installed_security_probe.py'
    spec=importlib.util.spec_from_file_location('stock_lifecycle',path)
    probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
    proof=probe.capture_installed_security('codex',scenario='stock_lifecycle_'+mode)
    assert proof['mode']==mode and proof['aliasWithdrawn'] and proof['lateOwnedParserResponseRefused']
    assert proof['nativeModalRemoved'] and proof['noApprovalKeyDelivered'] and proof['publicCookieCsrf']
    assert proof['nativeAdmitted']==proof['nativeProcessed']==proof['mainProcessed']==2
    assert len(proof['providerResponseAttempts'])==(1 if mode=='chat' else 0)
    record_property('native_stock_lifecycle',json.dumps(proof,sort_keys=True))
