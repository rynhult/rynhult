"""Joint real native/production ownership proof, with explicit reverse blockers."""

import importlib.util
import json
from pathlib import Path

import pytest


@pytest.mark.relay_native
def test_actual_sdk_to_tui_uses_hub_plan_same_id_and_refuses_unknown_reverse(record_property):
    path=Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/installed_security_probe.py'
    spec=importlib.util.spec_from_file_location('handover_probe',path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result=module.capture_installed_security('claude',scenario='handover')
    for key in ('productionHubOwnershipPlan','actualSDKToTUI','sdkWholeTreeExitedBeforeTUI',
                'exactResumeArgument','nativeHistoryVisible','currentPersonApiAuth',
                'revokedPersonRefusedBeforeStop','unapprovedSubscriptionRefusedBeforeStop',
                'unknownNativeDraftRefusedWithoutReplacement','productionEmptyTreeStopped'):
        assert result[key] is True, key
    for key in ('productionFrozenMetadataPolicy','mutableSettingsScopesExcluded','strictEmptyMcpConfig','mcpTamperingRefused'):
        assert result[key] is True,key
    assert len(result['unqualifiedConfigAuthControlsRefused'])==5
    assert result['modelCalls']==1 and result['remaining']
    assert result['declaredCapabilities']=={} and result['terminalMediated'] is False
    record_property('enrolled_native_handover',json.dumps(result,sort_keys=True))


def test_factory_configuration_home_is_account_bound_after_inherited_environment_mask(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from acc.broker import claude_owner

    home = tmp_path/'generated-account'
    monkeypatch.setattr(claude_owner.pwd,'getpwuid',lambda _:SimpleNamespace(pw_dir=str(home)))
    monkeypatch.setenv('CLAUDE_CONFIG_DIR',str(tmp_path/'foreign-inherited-config'))
    factory = object.__new__(claude_owner.ClaudeOwnerFactory)
    factory.env, factory.guards = {}, {'guardrails.json':'sha256:'+'a'*64}
    spec = SimpleNamespace(spec={'run_dir':str(tmp_path/'run'),'session_id':'fixture','auth_mode':'api_key'})
    env = factory.environment(spec)
    assert env['HOME']==str(home)
    assert env['CLAUDE_CONFIG_DIR']==str(home/'.claude')
    assert list(tmp_path.iterdir())==[]  # deriving launch roots touches no settings
