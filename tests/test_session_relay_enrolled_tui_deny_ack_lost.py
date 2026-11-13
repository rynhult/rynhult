
import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.relay_native


def test_owned_tui_denial_application_lost_ack(record_property):
    path=Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/installed_security_probe.py'
    spec=importlib.util.spec_from_file_location('tui_deny_ack',path)
    probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
    proof=probe.capture_installed_security('claude',scenario='tui_deny_ack_lost')
    assert proof['productionEmptyTreeStopped'] and proof['denialApplicationAcknowledged']
    assert proof['isolatedHomeTranscript'] and proof['noReplay'] and proof['publicFirstValidCAS']
    assert not proof['unknownPreserved']
    assert proof['lostApplicationPublicationAck'] and proof['oneNativeAnswer']
    assert proof['originalDecisionReceipt']['state']=='accepted'
    record_property('native_tui_deny_ack',json.dumps(proof,sort_keys=True))
