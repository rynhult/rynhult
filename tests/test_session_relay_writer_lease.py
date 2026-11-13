"""Prospective exclusive writer; retained provider, no native correlation input."""
import asyncio

import pytest

from acc.session_relay.protocol import Actor, ActorKind, Channel, RelayError
from acc.session_relay.writer_lease import ExclusiveWriterGate, WriterRetirement

ALICE = Actor(ActorKind.PERSON, 'alice', Channel.CHAT)
BOB = Actor(ActorKind.PERSON, 'bob', Channel.PHONE)


async def allowed(_):
    return True


async def test_one_writer_and_old_delayed_requests_stay_with_retired_lease():
    gate = ExclusiveWriterGate(run_id='run', execution_identity='same-private-app-server')
    assert gate.snapshot()['writer'] is None and gate.lease is None
    async def retire(lease):
        return WriterRetirement(lease.handle, True, True, True, True, 'same-private-app-server')
    first = await gate.activate(actor=ALICE, view='terminal', authorize=allowed, retire=retire)
    gate.admit(first, actor=ALICE, view='terminal')
    second = await gate.activate(actor=BOB, view='chat', authorize=allowed, retire=retire)
    gate.admit(second, actor=BOB, view='chat')
    with pytest.raises(RelayError):
        gate.admit(first, actor=BOB, view='chat')
    with pytest.raises(RelayError):
        gate.admit(first, actor=ALICE, view='terminal')
    assert gate.execution_identity == 'same-private-app-server'


@pytest.mark.parametrize('missing', ['idle', 'requests_settled', 'channel_closed'])
async def test_no_lease_transfer_without_each_positive_retirement_proof(missing):
    gate = ExclusiveWriterGate(run_id='run', execution_identity='private')
    async def retire(lease):
        values = dict(idle=True, draft_empty=True, requests_settled=True, channel_closed=True)
        values[missing] = False
        return WriterRetirement(lease.handle, execution_identity='private', **values)
    first = await gate.activate(actor=ALICE, view='terminal', authorize=allowed, retire=retire)
    with pytest.raises(RelayError):
        await gate.activate(actor=BOB, view='chat', authorize=allowed, retire=retire)
    assert gate.lease is first and gate.recovery_required


async def test_legacy_nonempty_proof_does_not_establish_readonly_client_preservation():
    gate = ExclusiveWriterGate(run_id='run', execution_identity='private')
    async def retire(lease):
        return WriterRetirement(lease.handle,True,False,True,True,'private')
    first = await gate.activate(actor=ALICE,view='terminal',authorize=allowed,retire=retire)
    with pytest.raises(RelayError):
        await gate.activate(actor=BOB,view='chat',authorize=allowed,retire=retire)
    assert gate.lease is first and gate.recovery_required


async def test_new_writer_revoked_during_retirement_never_gets_input():
    gate = ExclusiveWriterGate(run_id='run', execution_identity='private')
    revoked = False
    async def auth(_):
        return not revoked
    async def retire(lease):
        nonlocal revoked
        await asyncio.sleep(0)
        revoked = True
        return WriterRetirement(lease.handle, True, True, True, True, 'private')
    first = await gate.activate(actor=ALICE, view='terminal', authorize=auth, retire=retire)
    with pytest.raises(RelayError):
        await gate.activate(actor=BOB, view='chat', authorize=auth, retire=retire)
    assert gate.lease is first and gate.recovery_required


async def test_nonempty_draft_without_permanent_readonly_and_fresh_client_proof_still_refuses():
    # Josh's approved rule permits retaining a draft, not rebinding its mutable
    # native client. The old retirement structure lacks both required client
    # identities/proofs. Until the new native composition supplies them, refuse;
    # this is a missing-proof gate, not an empty-draft product requirement.
    gate = ExclusiveWriterGate(run_id='run', execution_identity='private')
    async def old_proof(lease):
        return WriterRetirement(lease.handle, True, False, True, True, 'private')
    first = await gate.activate(actor=ALICE, view='terminal', authorize=allowed, retire=old_proof)
    with pytest.raises(RelayError):
        await gate.activate(actor=BOB, view='chat', authorize=allowed, retire=old_proof)
    assert gate.lease is first and gate.recovery_required


@pytest.mark.parametrize('old,new,retained,accepted', [
    ('old', 'new', True, True), ('same', 'same', True, False),
    (None, 'new', True, False), ('old', None, True, False), ('old', 'new', False, False),
])
async def test_nonempty_retirement_requires_retained_draft_and_distinct_client_proof(old, new, retained, accepted):
    # Structural gate test only; native production composition is qualified separately.
    gate = ExclusiveWriterGate(run_id='run', execution_identity='private')
    async def retire(lease):
        return WriterRetirement(lease.handle, True, False, True, True, 'private', old, new, retained)
    first = await gate.activate(actor=ALICE, view='terminal', authorize=allowed, retire=retire)
    if accepted:
        second = await gate.activate(actor=BOB, view='chat', authorize=allowed, retire=retire)
        with pytest.raises(RelayError):
            gate.admit(first, actor=ALICE, view='terminal')
        gate.admit(second, actor=BOB, view='chat')
    else:
        with pytest.raises(RelayError):
            await gate.activate(actor=BOB, view='chat', authorize=allowed, retire=retire)
        assert gate.lease is first and gate.recovery_required

async def test_idle_take_keeps_old_draft_readonly_and_gives_new_writer_fresh_input():
    gate = ExclusiveWriterGate(run_id='same-run', execution_identity='same-private-provider')
    old = {'draft':'unsent previous text', 'readonly':False}
    new = {'draft':''}
    async def retire(lease):
        old['readonly'] = True
        return WriterRetirement(lease.handle, True, False, True, True,
            'same-private-provider', draft_retained_readonly=True, new_input_empty=True)
    first = await gate.activate(actor=ALICE, view='terminal', authorize=allowed, retire=retire)
    second = await gate.activate(actor=BOB, view='chat', authorize=allowed, retire=retire)
    assert old == {'draft':'unsent previous text', 'readonly':True}
    assert new['draft'] == ''
    assert second.run_id == first.run_id == 'same-run'
    assert gate.execution_identity == 'same-private-provider'
    with pytest.raises(RelayError):
        gate.admit(first, actor=ALICE, view='terminal')
    gate.admit(second, actor=BOB, view='chat')


@pytest.mark.parametrize('retained,fresh',[(False,True),(True,False),(False,False)])
async def test_nonempty_retirement_requires_both_preservation_and_fresh_input(retained,fresh):
    gate = ExclusiveWriterGate(run_id='run', execution_identity='private')
    async def retire(lease):
        return WriterRetirement(lease.handle,True,False,True,True,'private',retained,fresh)
    first = await gate.activate(actor=ALICE,view='terminal',authorize=allowed,retire=retire)
    with pytest.raises(RelayError):
        await gate.activate(actor=BOB,view='chat',authorize=allowed,retire=retire)
    assert gate.lease is first and gate.recovery_required and gate.epoch==1
