"""One real offline pinned-owner round trip, with no declaration/flag overrides."""
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.mark.relay_native
def test_codex_owner_preview_chat_terminal_chat(record_property):
    path=Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/installed_security_probe.py'
    spec=importlib.util.spec_from_file_location('codex_preview_probe',path)
    probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
    proof=probe.capture_installed_security('codex',scenario='codex_preview')
    for key in ['productionGrant','noQualificationOverrides','publicCookieCsrf','chatTerminalChat','oneExecutionOwner',
        'oldClientReadOnly','freshInputEmpty','receiptsUnchanged','noAutomaticTypingOrReplay','excludedFlagsOff',
        'productionOwnerStopped','nativeEmptyTreeCleanup']:
        assert proof[key] is True,(key,proof)
    assert proof['modelCalls']==3 and proof['terminalProcessed']==2 and proof['retainedDraft']
    assert proof['terminalMediated'] is False
    record_property('codex_owner_preview',json.dumps(proof,sort_keys=True))
