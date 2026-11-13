"""Exact decoder plus conclusive receipt, never a Stop/socket-drain shortcut."""
import json
from dataclasses import replace
from pathlib import Path

import pytest

from acc.session_relay import protocol as p
from acc.session_relay.adapters.base import AdapterOperation, InputGrant, TerminalRun
from acc.session_relay.claude_draft import ClaudeDraft


def test_browser_command_prefixes_match_python_source_of_truth():
    root = Path(__file__).resolve().parents[1]
    source = json.loads((root / 'src/acc/session_relay/text_prefixes.json').read_text())
    browser = json.loads((root / 'web/src/lib/sessionRelayPrefixes.json').read_text())
    assert browser == source


def fixture():
    actor = p.Actor(p.ActorKind.PERSON, 'alex', p.Channel.TERMINAL)
    grant = InputGrant(TerminalRun('fixture', 'run1'), 'fixture-tab', actor, 'opaque-fixture', 2, p.Fence(1, 'b', 'h'))
    boundary = ClaudeDraft(grant, 'native-fixture')
    return grant, boundary


def operation(grant, text='exact fixture prompt'):
    params = {'text': text, 'expectedControlRevision': grant.epoch}
    return AdapterOperation('operation', 'fixture', 'run1', 'turn.submit', p.operation_digest('turn.submit', params),
                            grant.actor, grant.fence, 999999, params)


def receipt(op, state=p.ReceiptState.ACCEPTED):
    return p.Receipt(op.operation_id, op.session_id, op.run_id, op.method, op.payload_digest, op.actor, state)


def test_partial_draft_and_decoded_prompt_without_receipt_cannot_drain():
    grant, boundary = fixture()
    boundary.reserve(grant, 1, b'exact fixture prompt')
    with pytest.raises(p.RelayError):
        boundary.drain(grant)
    boundary.reserve(grant, 2, b'\r')
    boundary.observe_prompt('native-fixture', p.sha256_digest(b'exact fixture prompt'))
    boundary.bind_operation(operation(grant))
    with pytest.raises(p.RelayError):
        boundary.confirm(receipt(operation(grant), p.ReceiptState.UNCERTAIN))
    with pytest.raises(p.RelayError):
        boundary.drain(grant)
    assert boundary.processed == 0 and boundary.draft == 'exact fixture prompt'


def test_exact_native_decoder_and_matching_conclusive_receipt_prove_empty_drain_and_freeze():
    grant, boundary = fixture()
    boundary.reserve(grant, 1, b'exact fixture prompt\r')
    boundary.observe_prompt('native-fixture', p.sha256_digest(b'exact fixture prompt'))
    op = operation(grant)
    boundary.bind_operation(op)
    boundary.confirm(receipt(op))
    assert p.to_wire(boundary.drain(grant)) == {'epoch': 2, 'admitted': 1, 'processed': 1}
    with pytest.raises(p.RelayError):
        boundary.reserve(grant, 2, b'late input')


@pytest.mark.parametrize('change', ['native', 'text', 'actor', 'run', 'digest', 'fence'])
def test_foreign_decoder_or_operation_cannot_advance_processing(change):
    grant, boundary = fixture()
    boundary.reserve(grant, 1, b'exact fixture prompt\r')
    op = operation(grant)
    with pytest.raises(p.RelayError):
        if change in ('native', 'text'):
            boundary.observe_prompt('other' if change == 'native' else 'native-fixture',
                                    p.sha256_digest(b'other' if change == 'text' else b'exact fixture prompt'))
        else:
            boundary.observe_prompt('native-fixture', p.sha256_digest(b'exact fixture prompt'))
            edits = {'actor': {'actor': replace(grant.actor, principal='other')}, 'run': {'run_id': 'other'},
                     'digest': {'payload_digest': p.sha256_digest(b'other')}, 'fence': {'fence': p.Fence(2, 'b', 'h')}}
            boundary.bind_operation(replace(op, **edits[change]))
    assert boundary.processed == 0 and boundary.partial


def test_backspacing_local_draft_to_empty_does_not_invent_native_processing():
    grant, boundary = fixture()
    boundary.reserve(grant, 1, b'x')
    boundary.reserve(grant, 2, b'\x7f')
    assert boundary.draft == '' and boundary.processed == 0
    with pytest.raises(p.RelayError):
        boundary.drain(grant)


def test_partial_transport_or_duplicate_decoder_stays_fenced():
    grant, boundary = fixture()
    boundary.reserve(grant, 1, b'exact fixture prompt\r')
    boundary.observe_prompt('native-fixture', p.sha256_digest(b'exact fixture prompt'))
    with pytest.raises(p.RelayError):
        boundary.observe_prompt('native-fixture', p.sha256_digest(b'exact fixture prompt'))
    with pytest.raises(p.RelayError):
        boundary.reserve(grant, 2, b'retry')
    assert boundary.partial and boundary.processed == 0


@pytest.mark.parametrize('state', [p.ReceiptState.FAILED, p.ReceiptState.SUBMITTING, p.ReceiptState.UNCERTAIN])
def test_nonaccepted_result_never_claims_native_editor_is_empty(state):
    grant, boundary = fixture()
    boundary.reserve(grant, 1, b'exact fixture prompt\r')
    boundary.observe_prompt('native-fixture', p.sha256_digest(b'exact fixture prompt'))
    op = operation(grant)
    boundary.bind_operation(op)
    with pytest.raises(p.RelayError):
        boundary.confirm(receipt(op, state))
    assert not boundary.verified_empty and boundary.processed == 0


def test_completed_operation_cannot_be_rebound_to_identical_later_text():
    grant, boundary = fixture()
    op = operation(grant)
    boundary.reserve(grant, 1, b'exact fixture prompt\r')
    boundary.observe_prompt('native-fixture', p.sha256_digest(b'exact fixture prompt'))
    boundary.bind_operation(op)
    boundary.confirm(receipt(op))
    boundary.reserve(grant, 2, b'exact fixture prompt\r')
    boundary.observe_prompt('native-fixture', p.sha256_digest(b'exact fixture prompt'))
    with pytest.raises(p.RelayError):
        boundary.bind_operation(op)
    assert boundary.processed == 1 and boundary.partial



def completed_boundary():
    grant,boundary=fixture()
    op=operation(grant)
    boundary.reserve(grant,1,b'exact fixture prompt\r')
    boundary.observe_prompt('native-fixture',p.sha256_digest(b'exact fixture prompt'))
    boundary.bind_operation(op)
    boundary.confirm(receipt(op))
    return grant,boundary


def test_nonempty_unsent_client_freezes_read_only_without_fabricating_processed_or_empty_state():
    grant,boundary=completed_boundary()
    boundary.reserve(grant,2,b'Keep this unsent draft')
    mark=boundary.freeze_for_handover(grant)
    assert (mark.admitted,mark.processed)==(2,1)
    assert boundary.retained_client.draft=='Keep this unsent draft'
    assert boundary.retained_client.read_only and boundary.frozen and not boundary.verified_empty
    assert boundary.freeze_for_handover(grant)==mark
    with pytest.raises(p.RelayError):boundary.reserve(grant,3,b'\r')
    with pytest.raises(p.RelayError):boundary.drain(grant)


@pytest.mark.parametrize('failure',['initial','enter','partial','foreign'])
def test_retained_client_never_admits_unknown_initial_pending_enter_partial_or_foreign_input(failure):
    grant,boundary=fixture() if failure=='initial' else completed_boundary()
    sequence=boundary.admitted+1
    boundary.reserve(grant,sequence,b'Must not replay'+(b'\r' if failure=='enter' else b''))
    if failure=='partial':boundary.poison()
    candidate=replace(grant,attachment_id='foreign-tab') if failure=='foreign' else grant
    with pytest.raises(p.RelayError):boundary.freeze_for_handover(candidate)
    assert boundary.retained_client is None


@pytest.mark.parametrize('data', [b'\x08', b'\x7f', b'a\x7f\x7f', *[c.encode() for c in '/!?#@&']])
@pytest.mark.parametrize('provider', ['codex', 'claude'])
def test_security_a3_empty_backspace_and_shared_command_prefixes(provider, data):
    from types import SimpleNamespace

    from acc.session_relay.claude_draft import project_text_frame
    from acc.session_relay.native_input_host import NativeInputHost
    with pytest.raises(p.RelayError):
        if provider == 'claude':
            project_text_frame('', data)
        else:
            NativeInputHost._draft(SimpleNamespace(draft=''), data)
