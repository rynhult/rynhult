"""Actual native question and first-valid authenticated public review race."""

import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.relay_native


def test_native_tui_question_has_one_public_hub_reviewer_winner_without_replay(record_property):
    path=Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/installed_security_probe.py'
    spec=importlib.util.spec_from_file_location('public_decisions_probe',path)
    probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
    result=probe.capture_installed_security('claude',scenario='public_decisions')
    for key in ('realCookieCsrfAuthentication','nativeTuiQuestion','publicFirstValidCAS','loserFailedBeforeDispatch',
            'exactReviewContext','unknownPreserved','noReplay','productionEmptyTreeStopped'):
        assert result[key] is True,key
    assert result['publicReviewers']==2
    record_property('native_public_decision_race',json.dumps(result,sort_keys=True))
