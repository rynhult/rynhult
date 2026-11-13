
import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.relay_native


def test_pinned_stock_exact_resolution_preserves_the_other_native_question(record_property):
    path=Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/installed_security_probe.py'
    spec=importlib.util.spec_from_file_location('stock_withdrawal',path)
    probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
    proof=probe.capture_installed_security('codex',scenario='stock_withdrawal')
    assert proof['exactWithdrawal'] and proof['concurrentOffers']
    assert proof['withdrawal']['lateOwnedResponseRefused'] and proof['withdrawal']['otherModalShownBeforeKey']
    assert proof['withdrawal']['applicationAcknowledgementClaimed'] is False
    assert proof['withdrawal']['otherAlias']==proof['stockDecoderKeys'][0]['alias']
    assert [(r['key'],r['choice']) for r in proof['stockDecoderKeys']]==[('escape','cancel')]
    assert proof['noProviderResponseCalls'] and proof['noInputGrantOrActorInferred']
    assert proof['productionOwnerStopped'] and proof['postStartupLocalEditDecodedAndCleared']
    record_property('native_stock_withdrawal',json.dumps(proof,sort_keys=True))
