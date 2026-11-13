"""Unknown old/partial trees reserve identity while every releasable FD closes."""
import os
from types import SimpleNamespace

import pytest

from acc.broker.relay_ownership import ExclusiveRuntime
from acc.broker.relay_runtime import RuntimeTransportFactory
from acc.broker.session_relay import AdmissionJournal, PayloadStaging
from acc.session_relay import protocol as p


@pytest.mark.parametrize(('old_exited', 'partial_exited'), [(False, True), (True, False), (True, True)])
@pytest.mark.parametrize('replacement', [False, True])
async def test_quarantine_keeps_unproved_identity_without_stranding_resources(tmp_path, old_exited, partial_exited, replacement):
    factory = RuntimeTransportFactory(SimpleNamespace(), owned_view_factory=None)
    state = ExclusiveRuntime(factory, 'fixture-enrollment')
    state.key = ('fixture', 'run1')
    factory.active[state.key] = state
    calls = []

    def owner(name, proved):
        async def close():
            calls.append(name)
            if not proved:
                raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            if name == 'old':
                raise RuntimeError('Unrelated controller cleanup fault after proven exit')
        async def exited():
            return proved
        return SimpleNamespace(close=close, exited=exited)

    old = owner('old', old_exited)
    state.controller = SimpleNamespace(factory=old, close=old.close)
    state.owners = owner('partial', partial_exited)
    state.application_proofs = SimpleNamespace(close=lambda: calls.append('proof'))
    state.service_operations['service'] = 'stage'
    state.staging = PayloadStaging(tmp_path / 'staging')
    state.journal = AdmissionJournal(tmp_path / 'journal')
    descriptors = state.staging.fd, state.journal.fd
    sentinel = object()
    if replacement:
        factory.active[state.key] = sentinel
    with pytest.raises((p.RelayError, RuntimeError)):
        await state.close()
    assert calls == ['old', 'partial', 'proof']
    assert state.retain_quarantine is (not (old_exited and partial_exited))
    assert not state.service_operations
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)
    if replacement:
        assert factory.active[state.key] is sentinel
    elif state.retain_quarantine:
        assert factory.active[state.key] is state and state.stopping and not state.ready
    else:
        assert not factory.active
    with pytest.raises((p.RelayError, RuntimeError)):
        await state.close()
    assert calls == ['old', 'partial', 'proof']


async def test_unproved_replaced_view_retains_identity_even_with_proved_native_exit(tmp_path):
    factory = RuntimeTransportFactory(SimpleNamespace(), owned_view_factory=None)
    state = ExclusiveRuntime(factory, 'fixture-enrollment')
    state.key = ('fixture', 'run1')
    factory.active[state.key] = state
    async def close():
        raise p.RelayError(p.ErrorCode.STALE_RUN)
    async def exited():
        return True
    state.owners = SimpleNamespace(close=close, exited=exited, retirement_unproved=True)
    with pytest.raises(p.RelayError):
        await state.close()
    assert state.retain_quarantine and factory.active[state.key] is state
