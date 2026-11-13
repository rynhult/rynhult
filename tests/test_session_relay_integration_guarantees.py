"""Guarantees composed at the primary/G4-G6 integration conflicts."""
import asyncio
from types import SimpleNamespace

import pytest
from claude_agent_sdk import ResultMessage, ToolResultBlock, UserMessage
from test_session_relay_claude_sdk_application import ID, setup
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.broker.claude_hooks import ClaudeHookServer
from acc.session_relay.adapters.claude_agent import ClaudeAgentAdapter
from acc.session_relay.claude_hook import denial_message, exchange


@pytest.mark.parametrize('effect', ['deny', 'allow_once'])
async def test_permission_application_settles_while_single_reader_keeps_background_fenced(effect):
    ledger, answer = setup(effect)
    turn = ledger.current['operation']
    queue, events, completed = asyncio.Queue(), [], asyncio.Event()
    class Client:
        async def receive_messages(self):
            while (message := await queue.get()) is not None:
                yield message
    async def publish(event):
        events.append(event)
        if event['kind'] == 'turn.completed':
            completed.set()
    adapter = ClaudeAgentAdapter(policy=lambda *_: True, publish=publish)
    adapter.spec = SimpleNamespace(run_id=turn.run_id, spec={'session_id': turn.session_id},
        binding=SimpleNamespace(native_conversation='native-fixture'))
    adapter.client, adapter.applications = Client(), ledger
    adapter.attestation = SimpleNamespace(process_identity='fixture-process')
    adapter.active = True
    adapter.reader = asyncio.create_task(adapter._read(turn))
    try:
        await queue.put({'subtype': 'task_started', 'task_id': 'bg'})
        await queue.put(UserMessage([ToolResultBlock('tool-1', denial_message(ID) if effect == 'deny' else 'done',
            effect == 'deny')], uuid='result-uuid'))
        await queue.put(ResultMessage('success', 1, 1, False, 1, 'native-fixture', stop_reason='end_turn'))
        await asyncio.wait_for(completed.wait(), 3)
        assert (await ledger.observe_answer(answer, effect, timeout=.01))['outcome'] == 'accepted'
        assert adapter.background == {'bg'} and adapter.draining_background()
        assert not adapter.reader.done() and not adapter.failure
        await queue.put({'subtype': 'task_notification', 'task_id': 'bg'})
        await asyncio.wait_for(adapter.reader, 3)
        assert not adapter.background and not adapter.failure
        assert sum(e['kind'] == 'operation.accepted' for e in events) == 1
    finally:
        adapter.reader.cancel()
        await asyncio.gather(adapter.reader, return_exceptions=True)


@pytest.mark.parametrize('allowed', [False, True])
async def test_installed_guard_precedes_reviewed_sdk_background_and_tool_ledger(tmp_path, allowed):
    calls = []
    async def authorize():
        return True
    async def guard(payload):
        calls.append('guard')
        return allowed
    async def observe(payload):
        calls.append('observe')
    def background():
        calls.append('sdk')
        return True
    hooks = ClaudeHookServer(tmp_path/'hook', SimpleNamespace(run_id='run',
        binding=SimpleNamespace(native_conversation='native')), authorize, observe, observe,
        prepare_application=lambda *_: None, guard=guard, sdk_background=background, accept_pid=lambda pid:pid==__import__('os').getpid())
    try:
        await hooks.start()
        result = await asyncio.to_thread(exchange, hooks.directory/'owner-channel.json',
            {'session_id': 'native', 'hook_event_name': 'PreToolUse', 'tool_use_id': 'tool1',
             'prompt_id': 'prompt1', 'tool_name': 'Bash', 'tool_input': {'command': 'fixture', 'run_in_background': True}})
        assert result == ({'observed': True} if allowed else {'behavior': 'deny'})
        assert calls == (['guard', 'sdk', 'observe'] if allowed else ['guard'])
        assert bool(hooks.tool_requests) is allowed
    finally:
        await hooks.close()


@pytest.mark.parametrize('reference', ['a'*32, 'sha256:'+'b'*64])
@pytest.mark.parametrize('standalone', [False, True])
def test_primary_application_markers_and_enrolled_denial_references_keep_distinct_native_evidence(reference, standalone):
    import runpy

    from acc.session_relay import claude_hook
    render = runpy.run_path(claude_hook.__file__)['denial_message'] if standalone else denial_message
    text = render(reference)
    assert reference in text
    assert ('[relay-decision:' in text) is (len(reference) == 32)
    with pytest.raises(ValueError):
        render('unbound text')


async def test_final_publication_keeps_failed_turn_truth_and_command_file_evidence_observation(runtime_case):  # noqa: F811
    import hashlib

    from acc.session_relay import protocol as p
    c = runtime_case
    await c.launch.launch('abc123def456')
    state = next(iter(c.factory.active.values()))
    calls, observations = [], []
    original = state.peer.call
    async def record(method, params):
        calls.append((method, params))
    state.peer.call = record
    state.adapter.observe_file = lambda event: observations.append(('file', event))
    state.adapter.answer_evidence.observe = lambda event: observations.append(('answer', event)) or []
    state.adapter.turns['original-operation'] = 'native-turn'
    state.adapter.active_turn = 'original-operation'
    try:
        await state.publish_event({'method': 'item/completed', 'params': {
            'threadId': 'fixture-native-id', 'turnId': 'native-turn', 'item': {
                'type': 'userMessage', 'id': 'native-item', 'content': [{'type': 'text', 'text': 'Artificial user input'}]}}})
        await state.publish_event({'method': 'turn/completed', 'params': {
            'threadId': 'fixture-native-id', 'turn': {'id': 'native-turn', 'status': 'failed'}}})
        assert [kind for kind, _ in observations] == ['file', 'answer', 'file', 'answer']
        assert calls[0] == ('publication', {'kind': 'message', 'role': 'user',
            'itemId': hashlib.sha256(p.canonical_json(['native-turn', 'native-item'])).hexdigest(), 'text': 'Artificial user input'})
        assert calls[-1] == ('publication', {'kind': 'turn.failed', 'operationId': 'original-operation'})
        assert state.adapter.active_turn is None
    finally:
        state.peer.call = original


def test_clean_relay_launch_preserves_child_envelope_parent_and_sdk_guard(env, tmp_path, monkeypatch):
    import json
    import sys
    import time

    from test_broker import base_params

    from acc.broker import ops
    from acc.session_capabilities import normalize
    receipt, executable = tmp_path/'launch-receipt.json', tmp_path/'fake-owner'
    executable.write_text(f'#!{sys.executable}\nimport json,os,time\nfrom pathlib import Path\n'
        f'Path({str(receipt)!r}).write_text(json.dumps({{"pid":os.getpid(),'
        '"parent":os.environ.get("ACC_PARENT_SESSION_ID"),"guards":os.environ.get("ACC_GUARD_FILES"),'
        '"inherited":bool(os.environ.get("RELAY_INTEGRATION_SENTINEL"))}))\ntime.sleep(30)\n')
    executable.chmod(0o500)
    monkeypatch.setenv('RELAY_INTEGRATION_SENTINEL', 'artificial-parent-only')
    params = base_params(env, args=[], files={'session-capabilities.json': json.dumps({
        'effective': normalize(), 'parent': '111122223333'}), 'relay-sdk-settings.json': '{}'})
    try:
        ops.launch(env['broker'], params, inherit_environment=False, owner_executable=str(executable))
        deadline = time.monotonic()+5
        while not receipt.exists() and time.monotonic()<deadline:
            time.sleep(.01)
        value = json.loads(receipt.read_text())
        assert value['parent'] == '111122223333' and value['inherited'] is False
        assert 'session-capabilities.json=' in value['guards'] and 'relay-sdk-settings.json=' in value['guards']
        pane = ops.tmux(env['broker'], 'display-message', '-p', '-t', '='+params['tmux_name']+':', '#{pane_pid}', check=True)
        assert int(pane.stdout) == value['pid']
    finally:
        ops.kill(env['broker'], params['tmux_name'])


async def test_reconnected_read_uses_current_viewer_grant_while_final_capture_keeps_owner_authority():
    from acc.broker.relay_terminal_read import capture_owned, read
    from acc.session_relay.adapters.base import TerminalRun

    checks, captures = [], []
    before = {'sessionId': 'session', 'runId': 'run', 'bindingId': 'binding', 'epoch': 1}
    async def call(method, params):
        assert method == 'authority.terminal_read' and params == {'grant': 'reader'}
        checks.append('reader')
        return before
    class Observer:
        async def capture(self, run, **kwargs):
            assert run == TerminalRun('session', 'run')
            captures.append(bool(kwargs))
            if kwargs:
                assert await kwargs['authorize']()
            return {'text': 'retained native screen', 'truncated': False}
    runtime = SimpleNamespace(guard_check=lambda: checks.append('guard'),
        broker=SimpleNamespace(link=(SimpleNamespace(authority_epoch=1), 'link')),
        peer=SimpleNamespace(call=call, closed=False), spec=SimpleNamespace(spec={'session_id': 'session'}, run_id='run'),
        binding=SimpleNamespace(provider='claude', binding_id='binding'), owned_view=None,
        owners=SimpleNamespace(observer=Observer(), guard_check=lambda: checks.append('owner-guard')),
        controller=SimpleNamespace(switch=SimpleNamespace(view='terminal')))
    assert (await read(runtime, {'grant': 'reader'}))['text'] == 'retained native screen'
    assert checks.count('reader') == 3 and checks.count('owner-guard') == 2
    assert (await capture_owned(runtime, TerminalRun('session', 'run')))['text'] == 'retained native screen'
    assert captures == [True, False]


@pytest.mark.parametrize('fault', ['none', 'parent', 'expired', 'authority'])
def test_same_transaction_policy_reuse_preserves_child_fences_and_authority_scope(monkeypatch, fault):
    from acc.server import child_caps, security_settings, session_caps
    from acc.session_capabilities import normalize

    session = SimpleNamespace(project_id=1, owner='person', host='local', run_as='account', parent_session_id='parent')
    subject = SimpleNamespace(project_id=2, owner='other', host='node', run_as='other-account')
    policy = {'switches': dict.fromkeys(security_settings.REGISTRY, False)}
    seen = []
    def parent(db, row):
        seen.append('parent')
        if fault == 'parent':
            raise child_caps.Refused('parent proof changed')
    def resolve(db, **scope):
        seen.append(scope)
        return policy
    monkeypatch.setattr(child_caps, 'check', parent)
    monkeypatch.setattr(session_caps, 'effective', lambda *_: normalize({'expires_at': 1} if fault == 'expired' else {}))
    monkeypatch.setattr(security_settings, 'resolve', resolve)
    monkeypatch.setattr(security_settings, 'frozen_protections', lambda _: ({}, {}))
    monkeypatch.setattr(security_settings, 'saved_input_problem', lambda *_: None)
    kwargs = {'current': policy, 'authority': subject if fault == 'authority' else None}
    db = SimpleNamespace(get=lambda *_: None)
    if fault in ('parent', 'expired'):
        with pytest.raises(security_settings.Conflict):
            security_settings.check_generation(db, session, **kwargs)
        assert seen == ['parent']
    else:
        security_settings.check_generation(db, session, **kwargs)
        assert seen == (['parent', {'project_id': 2, 'owner': 'other', 'machine': 'node', 'account': 'other-account'}]
            if fault == 'authority' else ['parent'])
