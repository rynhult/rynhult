import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.relay_native


@pytest.mark.parametrize('tool',['read','write','edit'])
@pytest.mark.parametrize('mode',['failed','failed_lost'])
def test_failed_tool_body_acknowledges_permission_allow_without_replay(tool,mode,record_property):
    path=Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/installed_security_probe.py'
    spec=importlib.util.spec_from_file_location('failed_allow_probe',path)
    probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
    proof=probe.capture_installed_security('claude',scenario='claude_ack_'+tool+'_'+mode)
    assert proof['applicationConfirmed'] and proof['receipt']['state']=='accepted'
    assert any(v['event']=='PostToolUseFailure' and v['toolId']==proof['toolId'] for v in proof['hooks'])
    assert any(v['tool_use_id']==proof['toolId'] and v['is_error'] is True for v in proof['nativeToolResults'])
    assert proof['attempts']==['allow'] and proof['writes']==['allow']
    record_property('native_sdk_failed_allow',json.dumps(proof,sort_keys=True))
