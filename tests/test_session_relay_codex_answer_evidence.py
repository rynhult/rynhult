"""Exact choice-specific native lifecycle, never a write/cleanup heuristic."""
from types import SimpleNamespace

import pytest

from acc.session_relay import protocol as p
from acc.session_relay.codex_answer_evidence import CommandAnswerEvidence


def register(ledger,key='a',request_id=0,item='item-a'):
    ledger.register(key,{'id':request_id,'method':'item/commandExecution/requestApproval',
        'params':{'threadId':'thread','turnId':'turn','itemId':item}},p.sha256_digest(b'same command'))


def events(request_id=0,item='item-a',item_status='declined',turn_status='interrupted'):
    return [{'method':'serverRequest/resolved','params':{'threadId':'thread','requestId':request_id}},
        {'method':'item/completed','params':{'threadId':'thread','turnId':'turn',
            'item':{'id':item,'type':'commandExecution','status':item_status}}},
        {'method':'turn/completed','params':{'threadId':'thread','turn':{'id':'turn','status':turn_status}}}]


@pytest.mark.parametrize('choice,item_status,turn_status',[('cancel','declined','interrupted'),
    ('accept','completed','completed')])
def test_only_full_exact_choice_specific_native_lifecycle_confirms_application(choice,item_status,turn_status):
    ledger=CommandAnswerEvidence();register(ledger)
    ledger.sending('a',SimpleNamespace(operation_id='chosen-answer'),choice)
    sequence=events(item_status=item_status,turn_status=turn_status)
    assert ledger.observe(sequence[0])==[] and ledger.observe(sequence[1])==[]
    proof,=ledger.observe(sequence[2])
    assert proof['operationId']=='chosen-answer' and proof['choiceId']==choice
    assert ledger.observe(sequence[2])==[]  # one evidence publication, no dispatch/replay


@pytest.mark.parametrize('failure',['interrupt','item','request','thread','status','type','duplicate','before_send'])
def test_cleanup_wrong_item_or_request_guard_denial_and_duplicates_do_not_prove_a_chosen_allow(failure):
    ledger=CommandAnswerEvidence();register(ledger)
    sequence=events(item_status='completed',turn_status='completed')
    if failure=='before_send':
        ledger.observe(sequence[0])
        with pytest.raises(p.RelayError):ledger.sending('a',SimpleNamespace(operation_id='op'),'accept')
        return
    ledger.sending('a',SimpleNamespace(operation_id='op'),'accept')
    if failure=='interrupt':sequence=[sequence[0],sequence[2]];sequence[-1]['params']['turn']['status']='interrupted'
    elif failure=='item':sequence[1]['params']['item']['id']='foreign'
    elif failure=='request':sequence[0]['params']['requestId']=99
    elif failure=='thread':sequence[0]['params']['threadId']='foreign'
    elif failure=='status':sequence[1]['params']['item']['status']='declined'
    elif failure=='type':sequence[1]['params']['item']['type']='agentMessage'
    elif failure=='duplicate':sequence.insert(1,sequence[0])
    assert not any(ledger.observe(event) for event in sequence)


def test_identical_concurrent_inputs_cannot_exchange_native_item_or_operation_identity():
    ledger=CommandAnswerEvidence();register(ledger);register(ledger,'b',1,'item-b')
    ledger.sending('a',SimpleNamespace(operation_id='op-a'),'cancel')
    ledger.sending('b',SimpleNamespace(operation_id='op-b'),'cancel')
    result=[proof for event in events(1,'item-b') for proof in ledger.observe(event)]
    assert len(result)==1 and result[0]['operationId']=='op-b'


def test_two_native_questions_for_the_same_item_remain_ambiguous():
    ledger=CommandAnswerEvidence();register(ledger);register(ledger,'b',1)
    with pytest.raises(p.RelayError):ledger.sending('a',SimpleNamespace(operation_id='op-a'),'cancel')


@pytest.mark.parametrize('resolved_first',[False,True])
def test_unchosen_cleanup_closes_exact_question_without_inventing_an_answer(resolved_first):
    ledger=CommandAnswerEvidence();register(ledger)
    resolved,_,ended=events()
    sequence=[resolved,ended] if resolved_first else [ended,resolved]
    proofs=[proof for event in sequence for proof in ledger.observe(event)]
    assert proofs==[{'kind':'decision.closed','decisionId':'a','actionDigest':p.sha256_digest(b'same command')}]
    assert ledger.observe(resolved)==[]
    with pytest.raises(p.RelayError):ledger.sending('a',SimpleNamespace(operation_id='late'),'cancel')


@pytest.mark.parametrize('exit_code',[7,-9,None,0,True,'7'])
@pytest.mark.parametrize('turn_status',['completed','interrupted','failed'])
def test_failed_execution_acknowledges_allow_only_with_typed_nonzero_exit_and_completed_turn(exit_code,turn_status):
    ledger=CommandAnswerEvidence();register(ledger)
    ledger.sending('a',SimpleNamespace(operation_id='original'),'accept')
    sequence=events(item_status='failed',turn_status=turn_status)
    sequence[1]['params']['item']['exitCode']=exit_code
    proofs=[proof for event in sequence for proof in ledger.observe(event)]
    assert bool(proofs)==(type(exit_code) is int and exit_code!=0 and turn_status=='completed')
    if proofs:assert proofs[0]['operationId']=='original' and proofs[0]['choiceId']=='accept'


@pytest.mark.parametrize('failure',['none','no_item','no_resolution','interrupted','foreign'])
def test_file_failed_apply_acknowledges_only_exact_allow_after_actual_failed_item(failure):
    ledger=CommandAnswerEvidence()
    ledger.register('file',{'id':0,'method':'item/fileChange/requestApproval',
        'params':{'threadId':'thread','turnId':'turn','itemId':'item-a'}},p.sha256_digest(b'full file review'))
    ledger.sending('file',SimpleNamespace(operation_id='original'),'accept')
    sequence=events(item_status='failed',turn_status='completed')
    sequence[1]['params']['item']['type']='fileChange'
    if failure=='no_item':sequence.pop(1)
    elif failure=='no_resolution':sequence.pop(0)
    elif failure=='interrupted':sequence[-1]['params']['turn']['status']='interrupted'
    elif failure=='foreign':sequence[1]['params']['item']['id']='other-file'
    proofs=[proof for event in sequence for proof in ledger.observe(event)]
    assert bool(proofs)==(failure=='none')


def test_file_cancel_resolution_and_interruption_without_an_item_never_acknowledge_choice():
    ledger=CommandAnswerEvidence()
    ledger.register('file',{'id':0,'method':'item/fileChange/requestApproval',
        'params':{'threadId':'thread','turnId':'turn','itemId':'item-a'}},p.sha256_digest(b'file'))
    ledger.sending('file',SimpleNamespace(operation_id='original'),'cancel')
    resolved,_,finished=events()
    assert ledger.observe(resolved)==[] and ledger.observe(finished)==[]
