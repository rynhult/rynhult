"""Pinned ordinary-command approval profile; no CAS/expiry release qualification."""
from dataclasses import replace

import pytest
from test_session_relay_codex import adapter, operation
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.server.models import RelayDecision
from acc.session_relay.adapters.base import Outcome
from acc.session_relay.protocol import CapabilityEntry, RelayError, Support


def callback(**changes):
    return {'id': 0, 'method': 'item/commandExecution/requestApproval', 'params': {
        'kind': 'command', 'threadId': 'native1', 'turnId': 'native-turn1', 'itemId': 'native-item',
        'startedAtMs': 1234, 'environmentId': 'local', 'command': 'fixture command', 'cwd': '/fixture',
        'proposedExecpolicyAmendment': ['fixture'],
        'availableDecisions': ['accept', {'acceptWithExecpolicyAmendment': {'execpolicy_amendment': ['fixture']}},
                               'acceptForSession', 'cancel'], **changes}}


async def active_adapter():
    instance, owner = await adapter()
    await instance.submit(operation())
    return instance, owner


def register(instance, request):
    instance.register_decision('decision1', native_request=request,
        action_digest='sha256:' + '2' * 64, revision=1, expires_ms=2000)


async def test_pinned_metadata_never_turns_an_offered_amendment_into_a_hub_choice():
    instance, _ = await active_adapter()
    register(instance, callback())
    assert instance.decisions['decision1']['choices'] == frozenset({'accept', 'cancel'})


@pytest.mark.parametrize('changes', [
    {'kind': 'writeStdin'}, {'kind': 'humanCommand'}, {'environmentId': 'remote'},
    {'startedAtMs': True}, {'startedAtMs': -1}, {'proposedExecpolicyAmendment': {}},
    {'proposedExecpolicyAmendment': [1]}, {'proposedExecpolicyAmendment': ['fixture'] * 65},
    {'additionalPermissions': {'filesystem': 'all'}}, {'networkApprovalContext': {}},
    {'proposedNetworkPolicyAmendments': []}, {'approvalId': 'another-callback'},
    {'unknown': None}, {'availableDecisions': []}, {'availableDecisions': 'accept'},
    {'availableDecisions': {'accept': True}}, {'availableDecisions': ['acceptForSession']},
    {'availableDecisions': [{'acceptWithExecpolicyAmendment': {'execpolicy_amendment': ['fixture']}}]},
])
async def test_unqualified_command_families_or_missing_choices_never_register(changes):
    instance, _ = await active_adapter()
    with pytest.raises(RelayError):
        register(instance, callback(**changes))
    assert not instance.decisions


async def test_explicit_cancel_response_is_single_use_and_remains_uncertain():
    instance, owner = await active_adapter()
    instance.record = replace(instance.record, operations={**instance.record.operations,
        'decision.tool_approval': CapabilityEntry(Support.QUALIFIED, evidence=('artificial profile fixture',))})
    sent = []
    async def respond(identifier, result, *, before_write=None):
        if before_write is not None:
            await before_write()
        sent.append((identifier, result))
    owner.respond = respond
    register(instance, callback())
    params = {'decisionId': 'decision1', 'choiceId': 'acceptForSession', 'expectedRevision': 1,
              'actionDigest': 'sha256:' + '2' * 64}
    with pytest.raises(RelayError):
        await instance.reply_decision(operation('decision.reply', params, operation_id='decision-op'))
    assert not sent
    params['choiceId'] = 'cancel'
    result = await instance.reply_decision(operation('decision.reply', params, operation_id='decision-op'))
    assert result.outcome is Outcome.UNKNOWN and sent == [(0, {'decision': 'cancel'})]
    with pytest.raises(RelayError):
        await instance.reply_decision(operation('decision.reply', params, operation_id='another-op'))
    assert len(sent) == 1


async def test_hub_labels_native_cancel_as_turn_cancellation_with_deny_effect(runtime_case):  # noqa: F811
    case = runtime_case
    await case.launch.launch('abc123def456')
    case.launch.publication({'kind': 'decision', 'decisionId': 'native-decision', 'actionDigest': 'sha256:' + '2' * 64,
        'expiresAtMs': 9999999999999, 'choices': ['accept', 'cancel'], 'description': 'Command: artificial fixture'})
    with case.db() as db:
        decision = db.get(RelayDecision, ('abc123def456', 'native-decision')).decision
        choices = {choice['id']: choice for choice in decision['choices']}
        assert choices['cancel']['label'] == 'Cancel turn' and choices['cancel']['effect'] == 'deny'
        assert choices['accept']['effect'] == 'allow_once'


@pytest.mark.parametrize('params', [{}, {'command': 'fixture', 'cwd': None}, {'command': '', 'cwd': '/fixture'},
    {'command': 'x' * 4097, 'cwd': '/fixture'}, {'command': 'fixture', 'cwd': '/fixture', 'reason': 1}])
def test_missing_or_unbounded_command_context_cannot_create_a_blind_review(params):
    from acc.session_relay.native_approval import describe_command
    with pytest.raises(RelayError):
        describe_command(params)


def test_native_review_description_keeps_exact_command_and_directory():
    from acc.session_relay.native_approval import describe_command
    params = {'command': 'fixture command', 'cwd': '/fixture', 'reason': 'Artificial review'}
    assert describe_command(params) == 'Command:\nfixture command\nDirectory:\n/fixture\nReason:\nArtificial review'


@pytest.mark.parametrize('context', [None, '', ' \n'])
async def test_old_codex_producer_without_review_context_can_only_offer_denial(runtime_case, context):  # noqa: F811
    case = runtime_case
    await case.launch.launch('abc123def456')
    case.launch.publication({'kind': 'decision', 'decisionId': 'old-native-decision', 'actionDigest': 'sha256:' + '2' * 64,
        'expiresAtMs': 9999999999999, 'choices': ['accept', 'decline', 'cancel'], 'description': context})
    with case.db() as db:
        decision = db.get(RelayDecision, ('abc123def456', 'old-native-decision')).decision
        assert {choice['id'] for choice in decision['choices']} == {'decline', 'cancel'}


@pytest.mark.parametrize('description', [None, '', '  '])
async def test_absent_review_context_can_only_publish_denial(runtime_case, description):  # noqa: F811
    case = runtime_case
    await case.launch.launch('abc123def456')
    case.launch.publication({'kind': 'decision', 'decisionId': 'blind-native', 'actionDigest': 'sha256:' + '2' * 64,
        'expiresAtMs': 9999999999999, 'choices': ['accept', 'cancel'], 'description': description})
    with case.db() as db:
        decision = db.get(RelayDecision, ('abc123def456', 'blind-native')).decision
        assert [choice['id'] for choice in decision['choices']] == ['cancel']
