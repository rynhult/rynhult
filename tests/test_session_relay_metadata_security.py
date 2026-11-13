"""Mandatory policy shape and real pinned native fail-closed metadata boundary."""
import importlib.util
import json
import os
from pathlib import Path

import pytest

from acc.session_relay import protocol as p

POLICY=Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/claude_metadata_policy.py'
SPEC=importlib.util.spec_from_file_location('metadata_policy',POLICY)
policy=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(policy)


def test_metadata_policy_is_mandatory_pure_and_does_not_approve_tools(tmp_path):
    original={'disableAllHooks':False,'permissions':{'allow':['Read'],'deny':['Edit(./private/**)']},
        'sandbox':{'enabled':False,'allowUnsandboxedCommands':True,'filesystem':{'disabled':True}}}
    frozen=policy.freeze(original,str(tmp_path/'run'))
    policy.validate(frozen,str(tmp_path/'run'))
    assert original['sandbox']['enabled'] is False
    assert frozen['permissions']['allow']==['Read']
    assert 'Edit(~/.claude/projects/**)' in frozen['permissions']['deny']
    assert frozen['sandbox']['autoAllowBashIfSandboxed'] is False
    assert frozen['sandbox']['failIfUnavailable'] is True
    assert list(tmp_path.iterdir())==[]


@pytest.mark.parametrize('mutation', ['sandbox','deny','fallback','filesystem','read'])
def test_weaker_metadata_policy_refuses_before_native_launch(mutation):
    value=policy.freeze({'disableAllHooks':False},'/tmp/generated/run')
    if mutation=='sandbox':value['sandbox']['enabled']=False
    if mutation=='deny':value['permissions']['deny']=[]
    if mutation=='fallback':value['sandbox']['allowUnsandboxedCommands']=True
    if mutation=='filesystem':value['sandbox']['filesystem']['disabled']=True
    if mutation=='read':value['sandbox']['filesystem']['denyRead']=[]
    with pytest.raises(p.RelayError):policy.validate(value,'/tmp/generated/run')


@pytest.mark.relay_native
@pytest.mark.parametrize('mode', ['bash','write','edit','symlink','hardlink','proc_root','proc_fd','run_read','run_write',
    'write_symlink','edit_symlink','write_proc_root','edit_proc_fd'])
def test_real_native_metadata_boundary_denies_forgery_and_continues_workspace_work(tmp_path,record_property,mode):
    before={key:os.environ.get(key) for key in ('HOME','CLAUDE_CONFIG_DIR')}
    file=Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/metadata_security_probe.py'
    spec=importlib.util.spec_from_file_location('metadata_probe',file)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    result=module.capture(tmp_path,mode)
    assert before=={key:os.environ.get(key) for key in before}
    assert Path(result['hostWritesConfinedTo']).is_relative_to(tmp_path)
    assert result['writableFileMounts']==['/tmp']
    assert result['productionEnforced'] is False
    assert result['metadataUnchanged'] and not result['protectedHostSettingsMounted']
    assert result['sandboxDependencyMissing'] is False
    assert result['workspaceWriteSucceeded'] and result['modelCalls']==(4 if mode.startswith(('write','edit')) else 3)
    assert result['fileReadSucceededBeforeMutation']==mode.startswith(('write','edit'))
    assert result['attackDeniedInNativeResult'] or mode=='run_write' and result['runWriteIsolatedInPrivateMount']
    assert result['trustedRunMarkerUnchanged']
    assert result['broadMutationToolAllowCannotOverrideBoundary']
    record_property('native_metadata_boundary',json.dumps(result,sort_keys=True))
