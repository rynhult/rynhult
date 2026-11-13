import asyncio
import sys
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from acc.broker.relay_ownership import ExclusiveRuntime
from acc.session_relay import protocol as p


@pytest.mark.parametrize('field',['session_id','run_dir','settings_path','sdk_settings_path','mcp_config_path','plugin_path','append_prompt_path'])
async def test_exclusive_launch_refuses_foreign_paths_before_factory_side_effect(tmp_path,monkeypatch,field):
    import acc.broker.claude_owner as module
    sid='abc123def456';run=tmp_path/'runs'/sid
    spec={'session_id':sid,'run_dir':str(run),'ownership_mode':'exclusive_idle',
        'settings_path':str(run/'settings.json'),'sdk_settings_path':str(run/'relay-sdk-settings.json'),
        'mcp_config_path':str(run/'mcp.json'),'plugin_path':str(run/'plugin'),'append_prompt_path':str(run/'append-prompt.md')}
    spec[field]='../escape' if field=='session_id' else str(tmp_path/'outside')
    binding=p.Binding('binding','installation','node','account',str(tmp_path),'claude','native','a'*64)
    async def call(*_):return {'binding':asdict(binding),'runId':'run','spec':spec}
    state=ExclusiveRuntime.__new__(ExclusiveRuntime)
    state.peer=SimpleNamespace(call=call);state.enrollment='enrollment'
    state.config=SimpleNamespace(run_dir=tmp_path/'runs',providers={'claude':sys.executable})
    state.factory=SimpleNamespace(active={},lock=asyncio.Lock());touched=[]
    def factory(*_):
        touched.append(True);raise AssertionError('untrusted paths reached materializing factory')
    monkeypatch.setattr(module,'ClaudeOwnerFactory',factory)
    with pytest.raises(p.RelayError) as error:await state.launch()
    assert error.value.code is p.ErrorCode.INTEGRITY_FAILURE and not touched and not state.factory.active
    assert not (tmp_path/'outside').exists()
