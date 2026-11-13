"""Protected pre-write tool binding and typed native chosen-Allow evidence."""
import copy

import pytest
from test_session_relay_claude_admission import append, create

from acc.broker.claude_allow_evidence import NativeAllow
from acc.broker.claude_tool_binding import prompt_digest
from acc.session_relay import protocol as p


def reader(tmp_path):
    path,old=create(tmp_path);old.close()
    common={'sessionId':'native-fixture','isSidechain':False}
    append(path,[{**common,'uuid':'prompt','parentUuid':None,'type':'user','promptId':'query-1',
        'turnPosition':{'promptIndex':2,'turnIndex':3},'message':{'role':'user','content':'fixture'}},
        {**common,'uuid':'assistant','parentUuid':'prompt','type':'assistant',
        'message':{'role':'assistant','id':'api-message','content':[{'type':'tool_use','id':'tool-1','name':'Bash','input':{'command':'fixture'}}]}}])
    value=NativeAllow(path.parent.parent,path,'native-fixture');value.begin()
    binding=value.bind({'boundToolId':'tool-1','promptId':'query-1','toolName':'Bash',
        'promptDigest':prompt_digest({'session_id':'native-fixture','prompt_id':'query-1','tool_name':'Bash','tool_input':{'command':'fixture'}})})
    return path,value,binding


def records():
    common={'sessionId':'native-fixture','isSidechain':False,'parentUuid':'assistant'}
    return [{**common,'uuid':'allow','type':'attachment','attachment':{'type':'hook_permission_decision',
        'hookEvent':'PermissionRequest','decision':'allow','toolUseID':'tool-1'}},
        {**common,'uuid':'result','type':'user','promptId':'query-1','sourceToolAssistantUUID':'assistant',
        'permissionDecision':{'decision':'accept','source':'hook','reasonType':'hook','reasonCode':'native'},
        'toolUseResult':{'interrupted':False},'message':{'role':'user','content':[
            {'type':'tool_result','tool_use_id':'tool-1','is_error':False,'content':'native output'}]}}]


@pytest.mark.parametrize('failed',[False,True])
def test_exact_original_allow_application_once(tmp_path,failed):
    path,value,binding=reader(tmp_path)
    try:
        rows=records();rows[1]['message']['content'][0]['is_error']=failed;append(path,rows)
        assert p.valid_digest(value.applied(binding))
        assert value.evidence_binding['promptSequence']==[2,3]
        assert value.applied(binding) is None
    finally:value.close()


@pytest.mark.parametrize('fault',['missing_allow','missing_result','deny','foreign_tool','foreign_prompt',
    'foreign_source','non_hook','interrupted','plain_text','duplicate_allow','duplicate_result',
    'wrong_session','sidechain','rewritten_prefix','replace','symlink','forged_binding'])
def test_inexact_or_untrusted_allow_stays_unknown(tmp_path,fault):
    path,value,binding=reader(tmp_path)
    try:
        rows=records()
        if fault=='missing_allow':rows=rows[1:]
        elif fault=='missing_result':rows=rows[:1]
        elif fault=='deny':rows[0]['attachment']['decision']='deny'
        elif fault=='foreign_tool':rows[0]['attachment']['toolUseID']='tool-2'
        elif fault=='foreign_prompt':rows[1]['promptId']='query-2'
        elif fault=='foreign_source':rows[1]['sourceToolAssistantUUID']='other'
        elif fault=='non_hook':rows[1]['permissionDecision']['source']='config'
        elif fault=='interrupted':rows[1]['toolUseResult']['interrupted']=True
        elif fault=='plain_text':rows[1]['message']['content'][0]['type']='text'
        elif fault=='duplicate_allow':rows.append({**copy.deepcopy(rows[0]),'uuid':'allow-2'})
        elif fault=='duplicate_result':rows.append({**copy.deepcopy(rows[1]),'uuid':'result-2'})
        elif fault=='wrong_session':rows[1]['sessionId']='other'
        elif fault=='sidechain':rows[1]['isSidechain']=True
        elif fault=='forged_binding':binding={**binding,'transcriptDigest':p.sha256_digest(b'foreign')}
        if fault=='replace':path.unlink()
        elif fault=='symlink':other=path.with_suffix('.other');path.rename(other);path.symlink_to(other)
        append(path,rows)
        if fault=='rewritten_prefix':path.write_text(path.read_text().replace('api-message','api-changed'))
        try:assert value.applied(binding) is None
        except (OSError,p.RelayError):assert fault in ['wrong_session','sidechain','rewritten_prefix','replace','symlink','forged_binding']
    finally:value.close()


@pytest.mark.parametrize('fault',['none','foreign_session','foreign_tool','foreign_prompt','raw_digest'])
def test_owned_post_tool_completion_still_requires_protected_original_source(tmp_path,fault):
    from acc.broker.claude_tool_binding import Completion
    path,value,binding=reader(tmp_path)
    try:
        event=Completion('native-fixture','tool-1',binding['promptDigest'],'PostToolUse',p.sha256_digest(b'native post'))
        if fault=='foreign_session':event=Completion('foreign','tool-1',binding['promptDigest'],event.event,event.reference)
        elif fault=='foreign_tool':event=Completion(event.session,'tool-2',binding['promptDigest'],event.event,event.reference)
        elif fault=='foreign_prompt':event=Completion(event.session,event.tool,p.sha256_digest(b'foreign'),event.event,event.reference)
        elif fault=='raw_digest':event=event.reference
        if fault=='none':assert p.valid_digest(value.applied(binding,completion=event))
        else:
            with pytest.raises(p.RelayError):value.applied(binding,completion=event)
    finally:value.close()


@pytest.mark.parametrize('foreign_prompt',[False,True])
def test_native_prompt_sequence_handles_omitted_prerequisite_file_result(tmp_path,foreign_prompt):
    from acc.broker.claude_tool_binding import Completion
    path,value,binding=reader(tmp_path);value.close()
    import json
    rows=[json.loads(line) for line in path.read_text().splitlines()]
    rows[-1]['parentUuid']='omitted-prerequisite-result'
    if foreign_prompt:
        rows.insert(-1,{'sessionId':'native-fixture','isSidechain':False,'uuid':'foreign-prompt',
            'parentUuid':None,'type':'user','promptId':'different','turnPosition':{'promptIndex':3,'turnIndex':4},
            'message':{'role':'user','content':'different user prompt'}})
    path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
    value=NativeAllow(path.parent.parent,path,'native-fixture');value.begin()
    try:
        completion=Completion('native-fixture','tool-1',binding['promptDigest'],'PostToolUse',p.sha256_digest(b'post'))
        result=value.applied(binding,completion=completion)
        assert (result is not None)==(not foreign_prompt)
    finally:value.close()


def test_duplicate_native_prompt_identity_never_selects_a_sequence(tmp_path):
    from acc.broker.claude_tool_binding import Completion
    path,value,binding=reader(tmp_path)
    try:
        append(path,[{'sessionId':'native-fixture','isSidechain':False,'uuid':'duplicate-prompt',
            'parentUuid':None,'type':'user','promptId':'query-1','turnPosition':{'promptIndex':9,'turnIndex':10},
            'message':{'role':'user','content':'duplicate'}}])
        completion=Completion('native-fixture','tool-1',binding['promptDigest'],'PostToolUse',p.sha256_digest(b'post'))
        assert value.applied(binding,completion=completion) is None
    finally:value.close()
