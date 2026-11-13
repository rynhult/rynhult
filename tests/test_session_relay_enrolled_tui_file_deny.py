import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.relay_native


@pytest.mark.parametrize('tool',['Write','Edit'])
def test_native_tui_file_denial_application(tool,record_property):
    path=Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/installed_security_probe.py'
    spec=importlib.util.spec_from_file_location('tui_file_deny',path)
    probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
    proof=probe.capture_installed_security('claude',scenario='tui_'+tool.lower()+'_deny')
    assert proof['nativeTuiDeniedTool']==tool and proof['denialApplicationAcknowledged']
    assert proof['oneNativeAnswer'] and proof['originalFileRetained'] and proof['isolatedHomeTranscript']
    record_property('native_tui_file_deny',json.dumps(proof,sort_keys=True))
