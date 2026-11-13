import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.relay_native


@pytest.mark.parametrize('choice',['accept','cancel'])
def test_pinned_public_stock_key_has_original_actor_cas_and_application(record_property,choice):
    path=Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/installed_security_probe.py'
    spec=importlib.util.spec_from_file_location('stock_public',path)
    probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
    proof=probe.capture_installed_security('codex',scenario='stock_public' if choice=='accept' else 'stock_public_cancel')
    assert proof['choice']==choice
    assert proof['publicCookieCsrf'] and proof['conclusiveProviderApplication'] and proof['originalFrameActor']
    assert proof['nativeProcessedFrame']==proof['mainProcessedFrame']==3 and proof['noReplay']
    assert proof['receipt']['state']=='accepted' and len(proof['nativeResponseAttempts'])==1
    record_property('native_stock_public',json.dumps(proof,sort_keys=True))
