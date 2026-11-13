
import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.relay_native


def test_native_identical_read_questions_reconcile_late_without_answer_replay(record_property):
    path=Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/installed_security_probe.py'
    spec=importlib.util.spec_from_file_location('tui_read_concurrent',path)
    probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
    proof=probe.capture_installed_security('claude',scenario='tui_read_concurrent')
    for key in ('productionEmptyTreeStopped','nativeConcurrentIdenticalReadQuestions','foregroundUnknownBeforeNativeEvidence',
            'lateNativeEvidenceReconciled','publicationLostBeforeMainRetried','originalActorPayloadPreserved',
            'oneAnswerPerQuestion','noReplay','isolatedHomeTranscript','observerResourcesReclaimed'):
        assert proof[key],key
    record_property('native_tui_read_concurrent',json.dumps(proof,sort_keys=True))
