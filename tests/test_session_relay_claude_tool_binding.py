"""Native ID joining uses the approved exact prompt/input digest, never order."""
import copy

import pytest

from acc.broker.claude_tool_binding import ToolBindings, prompt_digest


def payload(**values):
    return {'session_id':'native','prompt_id':'prompt-1','tool_name':'Bash',
        'tool_input':{'command':'fixture','description':'artificial'},**values}


def test_canonical_key_and_unique_native_pretool_binding():
    ledger=ToolBindings();pre=payload(hook_event_name='PreToolUse',tool_use_id='tool-1')
    ledger.observe(pre)
    request=payload(tool_input={'description':'artificial','command':'fixture'})
    assert prompt_digest(request)==prompt_digest(pre) and ledger.bind(request)=='tool-1'
    pending={'promptDigest':prompt_digest(request),'boundToolId':ledger.bind(request),'nativeToolId':None}
    assert ledger.permits(pending,{'first':pending})
    assert not ledger.permits(pending,{'first':pending,'other':copy.deepcopy(pending)})
    ledger.observe(payload(hook_event_name='PreToolUse',tool_use_id='tool-2'))
    assert ledger.bind(request) is None and not ledger.permits(pending,{'first':pending})
    ledger.observe(payload(hook_event_name='PostToolUse',tool_use_id='tool-2'))
    assert ledger.bind(request) is None  # collision never silently resolves into permission


@pytest.mark.parametrize('field,value',[('session_id','other'),('prompt_id','other'),('tool_name','Read'),
    ('tool_input',{'command':'other'}),('prompt_id',None),('tool_input',None)])
def test_other_or_missing_context_never_inherits_pretool_id(field,value):
    ledger=ToolBindings();ledger.observe(payload(hook_event_name='PreToolUse',tool_use_id='tool-1'))
    assert ledger.bind(payload(**{field:value})) is None


def test_actual_permission_native_id_can_disambiguate_identical_inputs():
    ledger=ToolBindings()
    for identifier in ['tool-1','tool-2']:ledger.observe(payload(hook_event_name='PreToolUse',tool_use_id=identifier))
    request=payload(tool_use_id='tool-2')
    pending={'promptDigest':prompt_digest(request),'boundToolId':ledger.bind(request),'nativeToolId':'tool-2'}
    other={**pending,'boundToolId':'tool-1','nativeToolId':'tool-1'}
    assert pending['boundToolId']=='tool-2' and ledger.permits(pending,{'first':pending,'other':other})
    assert not ledger.permits(pending,{'first':pending,'duplicate':dict(pending)})
    assert ledger.bind(payload(tool_use_id='foreign')) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('late_collision',[False,True])
async def test_hook_refuses_ambiguous_allow_before_future_signal(tmp_path,late_collision):
    import asyncio
    from types import SimpleNamespace

    from acc.broker.claude_hooks import ClaudeHookServer
    from acc.session_relay import protocol as p
    from acc.session_relay.claude_hook import exchange

    events=[];ready=asyncio.Event()
    async def authorize():return True
    async def observe(_):pass
    async def publish(event):events.append(event);ready.set()
    server=ClaudeHookServer(tmp_path/'hook',SimpleNamespace(run_id='run',
        binding=SimpleNamespace(native_conversation='native')),authorize,publish,observe, accept_pid=lambda pid:pid==__import__('os').getpid())
    await server.start()
    channel=server.directory/'owner-channel.json'
    await asyncio.to_thread(exchange,channel,payload(hook_event_name='PreToolUse',tool_use_id='tool-1'))
    task=asyncio.create_task(asyncio.to_thread(exchange,channel,payload(hook_event_name='PermissionRequest')))
    try:
        await asyncio.wait_for(ready.wait(),3)
        card=events[0];pending=server.pending[card['decisionId']]
        async def collide():
            await asyncio.to_thread(exchange,channel,payload(hook_event_name='PreToolUse',tool_use_id='tool-2'))
        if not late_collision:await collide()
        async def verify():
            if late_collision:await collide()
            return True
        with pytest.raises(p.RelayError) as error:
            await server.answer(card['decisionId'],card['actionDigest'],'allow_once',verify)
        assert error.value.code is p.ErrorCode.INPUT_CONFLICT
        assert not pending['claimed'] and not pending['future'].done()
        async def verified():return True
        assert await server.answer(card['decisionId'],card['actionDigest'],'deny',verified)=={'outcome':'unknown'}
        assert await asyncio.wait_for(task,3)=={'behavior':'deny'}
    finally:
        await server.close();await asyncio.gather(task,return_exceptions=True)


def test_allow_hook_preserves_original_input_without_requesting_rewrite(monkeypatch,capsys):
    import io
    import json
    from types import SimpleNamespace

    from acc.session_relay import claude_hook

    original=payload(hook_event_name='PermissionRequest')
    monkeypatch.setattr(claude_hook.sys,'stdin',SimpleNamespace(buffer=io.BytesIO(json.dumps(original).encode())))
    monkeypatch.setattr(claude_hook,'exchange',lambda *_:{'behavior':'allow'})
    monkeypatch.setattr(claude_hook.sys,'argv',['hook','fixture-channel'])
    assert claude_hook.main()==0
    result=json.loads(capsys.readouterr().out)
    assert result=={'hookSpecificOutput':{'hookEventName':'PermissionRequest','decision':{'behavior':'allow'}}}


@pytest.mark.asyncio
async def test_explicit_ask_policy_refuses_allow_and_card_explains_it(tmp_path):
    import asyncio
    from types import SimpleNamespace

    from acc.broker.claude_hooks import ClaudeHookServer
    from acc.session_relay import protocol as p
    from acc.session_relay.claude_hook import exchange

    ready=asyncio.Event();cards=[]
    async def authorize():return True
    async def observed(_):pass
    async def publish(card):cards.append(card);ready.set()
    server=ClaudeHookServer(tmp_path/'hook',SimpleNamespace(run_id='run',
        binding=SimpleNamespace(native_conversation='native',workspace='/tmp')),
        authorize,publish,observed,allow_policy=False, accept_pid=lambda pid:pid==__import__('os').getpid())
    await server.start();channel=server.directory/'owner-channel.json'
    await asyncio.to_thread(exchange,channel,payload(hook_event_name='PreToolUse',tool_use_id='tool-1'))
    task=asyncio.create_task(asyncio.to_thread(exchange,channel,payload(hook_event_name='PermissionRequest')))
    try:
        await asyncio.wait_for(ready.wait(),3);card=cards[0]
        assert card['choices']==['decline'] and 'Allow is unavailable' in card['description']
        async def verify():return True
        with pytest.raises(p.RelayError):await server.answer(card['decisionId'],card['actionDigest'],'allow_once',verify)
        pending=server.pending[card['decisionId']];assert not pending['claimed'] and not pending['future'].done()
        await server.answer(card['decisionId'],card['actionDigest'],'deny',verify)
        assert await asyncio.wait_for(task,3)=={'behavior':'deny'}
    finally:await server.close();await asyncio.gather(task,return_exceptions=True)


@pytest.mark.parametrize('fault',['none','auto','foreign_id','foreign_prompt','foreign_input','interrupt'])
def test_post_tool_evidence_requires_original_sent_allow(fault):
    ledger=ToolBindings();pre=payload(hook_event_name='PreToolUse',tool_use_id='tool-1');ledger.observe(pre)
    pending={'boundToolId':'tool-1','promptDigest':prompt_digest(pre)}
    if fault!='auto':ledger.allow_sent(pending)
    event={**pre,'hook_event_name':'PostToolUse'}
    if fault=='foreign_id':event['tool_use_id']='tool-2'
    elif fault=='foreign_prompt':event['prompt_id']='other'
    elif fault=='foreign_input':event['tool_input']={'command':'other'}
    elif fault=='interrupt':event.update(hook_event_name='PostToolUseFailure',is_interrupt=True)
    ledger.observe(event)
    binding={'toolId':'tool-1','promptDigest':prompt_digest(pre)}
    assert (ledger.evidence(binding) is not None)==(fault=='none')
    ledger.retire(binding)
    assert ledger.evidence(binding) is None and not ledger.allowed


@pytest.mark.asyncio
async def test_collision_during_final_reply_authorization_denies_before_native_write(tmp_path):
    import asyncio
    from types import SimpleNamespace

    from acc.broker.claude_hooks import ClaudeHookServer
    from acc.session_relay.claude_hook import exchange

    ready=asyncio.Event();blocked=asyncio.Event();release=asyncio.Event();cards=[];held=False
    async def authorize():
        nonlocal held
        if not held and any(value['claimed'] for value in server.pending.values()):
            held=True;blocked.set();await release.wait()
        return True
    async def observe(_):pass
    async def publish(card):cards.append(card);ready.set()
    server=ClaudeHookServer(tmp_path/'hook',SimpleNamespace(run_id='run',
        binding=SimpleNamespace(native_conversation='native')),authorize,publish,observe, accept_pid=lambda pid:pid==__import__('os').getpid())
    await server.start();channel=server.directory/'owner-channel.json'
    await asyncio.to_thread(exchange,channel,payload(hook_event_name='PreToolUse',tool_use_id='tool-1'))
    task=asyncio.create_task(asyncio.to_thread(exchange,channel,payload(hook_event_name='PermissionRequest')))
    try:
        await asyncio.wait_for(ready.wait(),3);card=cards[0]
        async def verify():return True
        await server.answer(card['decisionId'],card['actionDigest'],'allow_once',verify)
        await asyncio.wait_for(blocked.wait(),3)
        await asyncio.to_thread(exchange,channel,payload(hook_event_name='PreToolUse',tool_use_id='tool-2'))
        release.set()
        assert await asyncio.wait_for(task,3)=={'behavior':'deny'}
        assert not server.tool_bindings.allowed and not server.tool_bindings.completed
    finally:release.set();await server.close();await asyncio.gather(task,return_exceptions=True)
