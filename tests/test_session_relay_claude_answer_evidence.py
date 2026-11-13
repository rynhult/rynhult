"""Qualified SDK evidence needs the whole exact lifecycle, never tool text."""
from types import SimpleNamespace

import pytest

from acc.session_relay.claude_answer_evidence import applied


def values(allow=False):
    identity=SimpleNamespace(tool='Bash',tool_use_id='tool-a')
    response={'behavior':'allow' if allow else 'deny','writeCompleted':True}
    result={'sessionId':'native','subtype':'success','isError':False,'uuid':'native-result',
        'permissionDenials':() if allow else (('Bash','tool-a'),)}
    kwargs={'current':True,'native_id':'native','behavior':'allow' if allow else 'deny',
        'tool_error':not allow,'post_tool':allow,'tool_seen':True}
    return identity,response,result,kwargs


@pytest.mark.parametrize('allow',[False,True])
def test_exact_denial_or_successful_allow_requires_explicit_final_native_report(allow):
    identity,response,result,kwargs=values(allow)
    assert applied(identity,response,result,**kwargs)


@pytest.mark.parametrize('failure',['cancel','wrong_run','error','missing_report','wrong_tool','write','choice','result','hook'])
def test_cancelled_foreign_incomplete_or_failed_evidence_never_applies_allow(failure):
    identity,response,result,kwargs=values(True)
    if failure=='cancel':kwargs['current']=False
    elif failure=='wrong_run':result['sessionId']='foreign'
    elif failure=='error':result['isError']=True
    elif failure=='missing_report':result['permissionDenials']=None
    elif failure=='wrong_tool':identity.tool='UnqualifiedTool'
    elif failure=='write':response['writeCompleted']=False
    elif failure=='choice':response['behavior']='deny'
    elif failure=='result':kwargs['tool_error']=True
    elif failure=='hook':kwargs['post_tool']=False
    assert not applied(identity,response,result,**kwargs)


def test_another_tools_denial_or_generic_tool_error_is_not_acknowledgement():
    identity,response,result,kwargs=values()
    result['permissionDenials']=(('Bash','foreign-tool'),)
    assert not applied(identity,response,result,**kwargs)


@pytest.mark.parametrize('tool',['Read','Write','Edit'])
@pytest.mark.parametrize('missing',['none','result','hook','error'])
def test_nonbash_native_omitted_error_flag_needs_exact_typed_result_and_positive_hook(tool,missing):
    identity,response,result,kwargs=values(True);identity.tool=tool
    kwargs['tool_error']=None
    if missing=='result':kwargs['tool_seen']=False
    elif missing=='hook':kwargs['post_tool']=False
    elif missing=='error':kwargs['tool_error']=True
    assert applied(identity,response,result,**kwargs)==(missing=='none')


@pytest.mark.parametrize('tool',['Glob','Grep'])
def test_unoffered_sdk_tool_families_cannot_gain_application_from_fabricated_complete_metadata(tool):
    identity,response,result,kwargs=values(True);identity.tool=tool
    assert not applied(identity,response,result,**kwargs)


@pytest.mark.parametrize('tool',['Read','Write','Edit'])
@pytest.mark.parametrize('case',['exact','no_result','no_failure_hook','denied','cancelled','both_hooks'])
def test_failed_tool_body_is_permission_application_only_with_exact_native_failure_lifecycle(tool,case):
    identity,response,result,kwargs=values(True);identity.tool=tool
    kwargs.update(tool_error=True,post_tool=False,post_failed=True)
    if case=='no_result':kwargs['tool_seen']=False
    elif case=='no_failure_hook':kwargs['post_failed']=False
    elif case=='denied':result['permissionDenials']=((tool,'tool-a'),)
    elif case=='cancelled':kwargs['current']=False
    elif case=='both_hooks':kwargs['post_tool']=True
    assert applied(identity,response,result,**kwargs)==(case=='exact')
