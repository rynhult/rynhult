"""Native owner replacement and retained fresh input through public cookie plans."""

import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.relay_native


def test_public_l1_native_replacement_preserves_original_receipts_and_fresh_input(record_property):
    path=Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/installed_security_probe.py'
    spec=importlib.util.spec_from_file_location('public_l1_probe',path)
    probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
    proof=probe.capture_installed_security('claude',scenario='public_l1')
    for key in ('publicL1CookiePlans','publicL1OriginalReceipts','publicL1PreclaimCancel','oldConnectionNeverRenewed',
            'newOwnerFreshPersonLifecycle','publicRetainedDraftVisible','nonemptyNativeDraftRetainedReadonly',
            'newWriterFreshInput','noReplay','productionEmptyTreeStopped'):
        assert proof[key] is True,key
    record_property('native_public_l1',json.dumps(proof,sort_keys=True))
