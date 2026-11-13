import importlib.util
import json
from pathlib import Path

import pytest

# Write's pre-hook overwrite modal has its own terminal-person handoff proof.
# Never auto-answer that native modal to manufacture a remote Allow profile.
pytestmark = pytest.mark.relay_native


@pytest.mark.parametrize('profile',['ack','read','edit'])
def test_pinned_public_tui_allow_binds_original_native_tool(record_property,profile):
    path=Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/installed_security_probe.py'
    spec=importlib.util.spec_from_file_location('tui_allow_ack',path)
    probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
    proof=probe.capture_installed_security('claude',scenario='tui_allow_'+profile)
    assert proof['allowApplicationAcknowledged'] and not proof['unknownPreserved'] and proof['noReplay']
    assert proof['allowBindings'][0]['nativeToolId'] is None and proof['allowBindings'][0]['boundToolId']
    record_property('native_tui_allow_ack',json.dumps(proof,sort_keys=True))
