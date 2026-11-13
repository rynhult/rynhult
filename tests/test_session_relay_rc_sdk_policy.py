import json
from types import SimpleNamespace

import pytest

from acc.broker.claude_owner import ClaudeOwnerFactory
from acc.session_relay import protocol as p
from acc.session_relay.claude_metadata_policy import freeze


@pytest.mark.parametrize('defect',['denyRead','denyWrite','disabled'])
def test_sdk_policy_checked_before_owner_materialization(tmp_path,monkeypatch,defect):
    sid='abc123def456';run=tmp_path/sid
    tui=freeze({'disableAllHooks':False,'hooks':{}},str(run))
    sdk=json.loads(json.dumps(tui))
    if defect=='disabled':sdk['sandbox']['enabled']=False
    else:sdk['sandbox']['filesystem'][defect]=[]
    spec=SimpleNamespace(run_id='run',spec={'session_id':sid,'run_dir':str(run),'ownership_mode':'exclusive_idle',
        'settings_path':str(run/'settings.json'),'sdk_settings_path':str(run/'relay-sdk-settings.json'),
        'auth_mode':'api_key','terminal_resume_args':['--setting-sources','','--strict-mcp-config','--mcp-config','mcp.json']})
    touched=[]
    def guard(*_):touched.append(True);raise AssertionError('unfrozen SDK settings reached installed guard')
    monkeypatch.setattr('acc.broker.claude_guard.InstalledGuard',guard)
    value={'env':{},'files':{'settings.json':json.dumps(tui),'relay-sdk-settings.json':json.dumps(sdk),'mcp.json':'{"mcpServers":{}}'}}
    with pytest.raises(p.RelayError) as error:
        ClaudeOwnerFactory(SimpleNamespace(config=SimpleNamespace(run_dir=tmp_path,hook_path=tmp_path/'hook')),spec,value,None)
    assert error.value.code is p.ErrorCode.INTEGRITY_FAILURE and not touched and not run.exists()
