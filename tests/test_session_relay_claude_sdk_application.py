"""Pinned SDK types, exact query/request and completed response; no transport ACK proof."""
from dataclasses import replace

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, ToolResultBlock, ToolUseBlock, UserMessage
from test_session_relay_claude_draft import fixture, operation

from acc.session_relay import protocol as p
from acc.session_relay.claude_hook import denial_message
from acc.session_relay.claude_sdk_application import SDKApplications

ID = 'a' * 32
INPUT = {'command': 'echo artificial'}


def setup(effect):
    grant, _ = fixture()
    turn = operation(grant)
    ledger = SDKApplications()
    ledger.begin(turn, 'native-fixture')
    ledger.observe(ledger.current, AssistantMessage([ToolUseBlock('tool-1', 'Bash', INPUT)], 'fixture'))
    ledger.request(ID, 'Bash', INPUT)
    params = {'decisionId': ID, 'choiceId': 'accept' if effect == 'allow_once' else 'decline',
              'expectedRevision': 0, 'actionDigest': p.sha256_digest(p.canonical_json({'tool': 'Bash', 'input': INPUT}))}
    answer = replace(turn, operation_id='answer', method='decision.reply', params=params,
                     payload_digest=p.operation_digest('decision.reply', params))
    ledger.bind(ID, answer, effect)
    return ledger, answer


@pytest.mark.parametrize('effect', ['deny', 'allow_once'])
@pytest.mark.parametrize('fault', ['none', 'marker', 'tool', 'unfinished', 'wrong_session', 'old_query', 'ambiguous', 'unbound'])
async def test_sdk_application_requires_exact_typed_result_and_completion(effect, fault):
    ledger, answer = setup(effect)
    content = denial_message(ID) if effect == 'deny' else 'artificial'
    result = UserMessage([ToolResultBlock('tool-1', content, effect == 'deny')], uuid='result-uuid')
    if fault == 'marker':
        result.content[0].content = 'an unrelated result'
        if effect == 'allow_once':
            result.content[0].is_error = True
    elif fault == 'tool':
        result.content[0].tool_use_id = 'other'
    elif fault == 'old_query':
        grant, _ = fixture()
        ledger.begin(replace(operation(grant), operation_id='replacement'), 'native-fixture')
    elif fault == 'ambiguous':
        ledger.observe(ledger.current, AssistantMessage([ToolUseBlock('tool-1', 'Bash', INPUT)], 'fixture'))
    elif fault == 'unbound':
        ledger.records[ID]['bound'] = None
        if effect == 'deny':
            ledger.records[ID]['answer'] = None
    ledger.observe(ledger.current, result)
    if fault != 'unfinished':
        ledger.observe(ledger.current, ResultMessage('success', 1, 1, False, 1,
            'another-session' if fault == 'wrong_session' else 'native-fixture', stop_reason='end_turn'))
    if effect == 'deny' and fault == 'unbound':
        with pytest.raises(p.RelayError):
            await ledger.observe_answer(answer, effect, timeout=.001)
        return
    observed = await ledger.observe_answer(answer, effect, timeout=.001)
    assert observed['outcome'] == ('accepted' if fault == 'none' else 'unknown')
    if fault == 'none':
        assert observed['application']['nativeToolId'] == 'tool-1'
        assert observed['application']['effect'] == effect
