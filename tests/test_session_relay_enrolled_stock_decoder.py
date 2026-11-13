import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.relay_native


@pytest.mark.parametrize('concurrent',[False,True])
def test_pinned_stock_enter_escape_decoder_and_owned_alias_boundary(record_property,concurrent):
    path=Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/installed_security_probe.py'
    spec=importlib.util.spec_from_file_location('stock_decoder',path)
    probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
    proof=probe.capture_installed_security('codex',scenario='stock_decoder_concurrent' if concurrent else 'stock_decoder')
    assert proof['productionOwnerStopped'] and proof['ownedPeerVerified'] and proof['decoderOnly']
    assert [(r['key'],r['choice']) for r in proof['stockDecoderKeys']]==[('enter','accept'),('escape','cancel')]
    assert proof['concurrentOffers'] is concurrent
    assert proof['postStartupLocalEditDecodedAndCleared']
    assert proof['foreignPeerAliasRefused'] and proof['noProviderResponseCalls'] and proof['noInputGrantOrActorInferred']
    record_property('native_stock_decoder',json.dumps(proof,sort_keys=True))
