"""Retained draft data never becomes a writable or transferable input token."""
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace

import pytest

from acc.session_relay import protocol as p
from acc.session_relay.adapters.base import InputGrant, TerminalRun
from acc.session_relay.claude_retained_client import RetainedClaudeClient


def grant():
    return InputGrant(TerminalRun('example-session','old-run'),'old-tab',
        p.Actor(p.ActorKind.PERSON,'alex',p.Channel.TERMINAL),'never-expose-grant',3,p.Fence(1,'b','h'))


def test_retained_client_is_immutable_without_advancing_processing_or_exposing_an_input_token():
    client=RetainedClaudeClient.capture(grant(),'native-id',3,2,'My unsent draft')
    assert client.read_only and client.admitted==3 and client.processed==2
    with pytest.raises(FrozenInstanceError):client.draft='replacement input'
    with pytest.raises(FrozenInstanceError):client.read_only=False
    assert not hasattr(client,'grant') and not hasattr(client,'write') and not hasattr(client,'send')
    assert 'My unsent draft' not in repr(client) and 'never-expose-grant' not in repr(client.proof())


@pytest.mark.parametrize('change',[
    {'run':TerminalRun('example-session','other-run')},{'attachment_id':'other-tab'},
    {'actor':p.Actor(p.ActorKind.PERSON,'blair',p.Channel.TERMINAL)},{'epoch':4}])
def test_retained_proof_cannot_be_rebound_to_another_client_or_person(change):
    original=RetainedClaudeClient.capture(grant(),'native-id',3,2,'same text')
    other=RetainedClaudeClient.capture(replace(grant(),**change),'native-id',3,2,'same text')
    assert original.proof()!=other.proof()


@pytest.mark.parametrize('admitted,processed,draft',[(1,0,'first draft'),(2,2,'already processed'),
    (3,2,'not\rsubmitted'),(3,2,'not\x1btext')])
def test_unknown_initial_or_submitted_bytes_are_not_a_retained_unsent_client(admitted,processed,draft):
    with pytest.raises(p.RelayError):RetainedClaudeClient.capture(grant(),'native-id',admitted,processed,draft)


def registry():
    from acc.server.session_relay.claude_terminal_grants import ClaudeTerminalGrants
    result=ClaudeTerminalGrants(SimpleNamespace(binding=SimpleNamespace(native_conversation='native-id')))
    result.record=SimpleNamespace(value=grant(),phase='active',pending=False,sequence=3,processed=2,
        entered_sequence=None,private_draft='My unsent draft')
    result.private_draft='My unsent draft'
    return result


def test_main_retains_only_the_exact_fully_delivered_unsent_projection_without_processing_it():
    current=registry();client=current.handover_client()
    assert client.draft=='My unsent draft' and client.read_only
    assert current.record.sequence==3 and current.record.processed==2 and not current.record.pending
    assert current.record.phase=='active' and current.retained_clients=={}


@pytest.mark.parametrize('failure',['pending','entered','registry_entered','projection','capacity','initial'])
def test_main_refuses_uncertain_or_submitted_draft_before_owner_stop(failure):
    current=registry()
    if failure=='pending':current.record.pending=True
    elif failure=='entered':current.record.entered_sequence=3
    elif failure=='registry_entered':current.entered_sequence=3
    elif failure=='projection':current.record.private_draft='different text'
    elif failure=='capacity':current.retained_clients={str(i):object() for i in range(16)}
    elif failure=='initial':current.record.processed=0
    with pytest.raises(p.RelayError):current.handover_client()
    assert current.record.phase=='active' and current.record.sequence==3


@pytest.mark.parametrize('failure',['missing','digest','watermark','changed_stream'])
async def test_main_rejects_unmatched_retention_before_the_destructive_checkpoint(failure):
    from acc.server.session_relay.exclusive_runtime import ExclusiveLaunch
    current=registry();client=current.handover_client()
    run=object.__new__(ExclusiveLaunch)
    run.terminal_grants=current
    run.plans={'opaque-plan':{'_retainedClient':client}}
    proof=client.proof()
    if failure=='missing':proof=None
    elif failure=='digest':proof={**proof,'draftDigest':p.sha256_digest(b'foreign')}
    elif failure=='watermark':proof={**proof,'processed':3}
    elif failure=='changed_stream':current.record.private_draft='Changed during transition'
    with pytest.raises(p.RelayError):
        await run.handle('authority.switch_checkpoint',{'plan':'opaque-plan','phase':'stopping',
            'evidence':{'retained_input':proof}})
    # No owner_hub/DB exists on this object: a destructive checkpoint was never reached.
    assert current.retained_clients=={} and current.record.processed==2


def test_revoking_current_input_cannot_erase_or_replay_the_retained_old_client():
    current=registry();client=current.handover_client();record=current.record
    current.retained_clients[(client.run_id,client.attachment_id)]=client
    current.invalidate()
    assert current.record is None and record.phase=='revoked'
    assert current.private_draft=='' and current.entered_sequence is None
    assert current.handover_client() is None  # no new input is synthesized from retained data
    assert current.retained_clients[(client.run_id,client.attachment_id)] is client
    assert client.read_only and client.draft=='My unsent draft' and client.processed==2
