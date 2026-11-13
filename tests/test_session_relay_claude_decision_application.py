"""Completed pinned native chain, exact action and private invocation marker required."""
from copy import deepcopy

import pytest
from test_session_relay_claude_admission import append, create, records
from test_session_relay_claude_draft import fixture, operation

from acc.session_relay import protocol as p
from acc.session_relay.claude_hook import denial_message

MARKER = 'a' * 32
INPUT = {'command': 'echo artificial', 'timeout': 1}


def denied_chain():
    rows = records()
    rows[2]['message'].update(content=[{'type': 'tool_use', 'id': 'native-tool-1', 'name': 'Bash', 'input': INPUT}],
                              stop_reason='tool_use')
    rows.append({'type': 'user', 'sessionId': 'native-fixture', 'isSidechain': False,
        'uuid': 'result-1', 'parentUuid': 'assistant-1', 'toolUseResult': 'Error: ' + denial_message(MARKER),
        'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'native-tool-1',
            'is_error': True, 'content': denial_message(MARKER)}]}})
    rows.append({'type': 'assistant', 'sessionId': 'native-fixture', 'isSidechain': False,
        'uuid': 'final-1', 'parentUuid': 'result-1', 'message': {'role': 'assistant',
            'id': 'provider-final', 'stop_reason': 'end_turn'}})
    return deepcopy(rows)


def test_exact_native_denial_does_not_consume_turn_admission_or_infer_other_choices(tmp_path):
    path, reader = create(tmp_path)
    grant, _ = fixture()
    try:
        reader.begin()
        append(path, denied_chain())
        reference = reader.denied_application(operation(grant), 'Bash', INPUT, MARKER)
        assert reference == {'nativeRef': 'native-fixture:user-1:provider-final:' + MARKER + ':native-tool-1',
                             'nativeToolId': 'native-tool-1'}
        # Observation may repeat, but admission is consumed exactly once by its own receipt path.
        assert reader.denied_application(operation(grant), 'Bash', INPUT, MARKER) == reference
        assert reader.evidence(operation(grant)).kind is p.EvidenceKind.PROVIDER_ADMISSION
    finally:
        reader.close()


@pytest.mark.parametrize('fault', ['marker', 'tool', 'input', 'native_tool', 'not_error', 'metadata',
    'input_type', 'role', 'unfinished', 'other_branch', 'duplicate_result', 'old_history', 'missing_use', 'same_action_wrong_id'])
def test_generic_errors_unfinished_chains_and_ambiguous_or_unbound_results_stay_unknown(tmp_path, fault):
    path, reader = create(tmp_path)
    grant, _ = fixture()
    try:
        rows = denied_chain()
        if fault == 'marker':
            rows[3]['message']['content'][0]['content'] = denial_message('b' * 32)
        elif fault == 'tool':
            rows[2]['message']['content'][0]['name'] = 'Write'
        elif fault == 'input':
            rows[2]['message']['content'][0]['input'] = {'command': 'different'}
        elif fault == 'input_type':
            rows[2]['message']['content'][0]['input'] = {'command': 'echo artificial', 'timeout': True}
        elif fault == 'role':
            rows[3]['message']['role'] = 'assistant'
        elif fault == 'native_tool':
            rows[3]['message']['content'][0]['tool_use_id'] = 'another-tool'
        elif fault == 'not_error':
            rows[3]['message']['content'][0]['is_error'] = False
        elif fault == 'metadata':
            rows[3]['toolUseResult'] = 'Error: an unrelated tool failed'
        elif fault == 'unfinished':
            rows[4]['message']['stop_reason'] = None
        elif fault == 'other_branch':
            rows[4]['parentUuid'] = 'assistant-1'
        elif fault == 'duplicate_result':
            rows[3]['message']['content'].append(deepcopy(rows[3]['message']['content'][0]))
        elif fault == 'missing_use':
            rows[2]['message']['content'] = []
        elif fault == 'same_action_wrong_id':
            rows[2]['message']['content'][0]['id'] = 'another-tool'
        if fault == 'old_history':
            append(path, rows)
            reader.begin()
        else:
            reader.begin()
            append(path, rows)
        with pytest.raises(p.RelayError):
            reader.denied_application(operation(grant), 'Bash', INPUT, MARKER)
    finally:
        reader.close()


def allowed_chain():
    rows = denied_chain()
    rows[3]['toolUseResult'] = {'stdout': 'artificial', 'stderr': '', 'interrupted': False}
    rows[3]['message']['content'][0].update(is_error=False, content='artificial')
    return rows


@pytest.mark.parametrize('fault', ['none', 'input', 'tool_id', 'error', 'missing_result', 'unfinished', 'ambiguous'])
def test_allow_requires_exact_prebound_tool_and_completed_provider_chain(tmp_path, fault):
    path, reader = create(tmp_path)
    grant, _ = fixture()
    rows = allowed_chain()
    if fault == 'input':
        rows[2]['message']['content'][0]['input'] = {'command': 'different'}
    elif fault == 'tool_id':
        rows[3]['message']['content'][0]['tool_use_id'] = 'unbound'
    elif fault == 'error':
        rows[3]['message']['content'][0]['is_error'] = True
    elif fault == 'missing_result':
        rows[3]['toolUseResult'] = None
    elif fault == 'unfinished':
        rows[4]['message']['stop_reason'] = None
    elif fault == 'ambiguous':
        rows[2]['message']['content'].append(deepcopy(rows[2]['message']['content'][0]))
    try:
        reader.begin()
        append(path, rows)
        if fault == 'none':
            assert reader.allowed_application(operation(grant), 'Bash', INPUT, MARKER, 'native-tool-1')['nativeToolId'] == 'native-tool-1'
        else:
            with pytest.raises(p.RelayError):
                reader.allowed_application(operation(grant), 'Bash', INPUT, MARKER, 'native-tool-1')
    finally:
        reader.close()
