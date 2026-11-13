"""Frozen enrollment and broker refusal before placement; fake CLI is explicit."""
import json
from types import SimpleNamespace

import pytest
from test_session_relay_ownership import ownership_case  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.broker.claude_owner import ClaudeOwnerFactory
from acc.session_relay import protocol as p
from acc.session_relay.claude_metadata_policy import freeze, validate


async def test_enrollment_binds_sandbox_settings_empty_mcp_and_immutable_sources(ownership_case):  # noqa: F811
    c=ownership_case
    spec=c.owned_factory.spec
    settings=json.loads(open(spec.spec['settings_path']).read())
    validate(settings,spec.spec['run_dir'])
    args=spec.spec['terminal_resume_args']
    assert args[args.index('--setting-sources')+1]==''
    assert '--strict-mcp-config' in args
    assert 'mcp.json' in c.owned_factory.guards
    assert c.owned_factory.current.client.options.setting_sources==[]
    assert c.owned_factory.current.client.options.extra_args['strict-mcp-config'] is None


@pytest.mark.parametrize('fault',['sandbox','mcp','sources','strict','missing_sources'])
def test_broker_refuses_weakened_boundary_before_materializing_any_run(tmp_path,fault):
    run=str(tmp_path/'run')
    settings=freeze({'disableAllHooks':False},run)
    args=['--settings',run+'/settings.json','--setting-sources','','--mcp-config',run+'/mcp.json','--strict-mcp-config']
    mcps={'mcpServers':{}}
    if fault=='sandbox':settings['sandbox']['enabled']=False
    if fault=='mcp':mcps['mcpServers']['external']={'command':'unqualified'}
    if fault=='sources':args[3]='user,project,local'
    if fault=='strict':args.remove('--strict-mcp-config')
    if fault=='missing_sources':args=args[:2]+args[4:]
    spec=SimpleNamespace(spec={'run_dir':run,'auth_mode':'api_key','terminal_resume_args':args})
    value={'env':{'ANTHROPIC_API_KEY':'offline-only'},'files':{
        'settings.json':json.dumps(settings),'mcp.json':json.dumps(mcps)}}
    with pytest.raises(p.RelayError):
        ClaudeOwnerFactory(SimpleNamespace(config=SimpleNamespace()),spec,value,None)
    assert list(tmp_path.iterdir())==[]
