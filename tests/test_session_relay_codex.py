"""Codex protocol fakes, exact attribution and real guard function fixture.

No CLI, model, daemon, executable guard hook or account state is touched.
"""
import asyncio
import importlib.machinery
import importlib.util
import json
from dataclasses import replace
from pathlib import Path

import pytest

from acc.session_relay.adapters.base import (
    AdapterOperation,
    AdmissionResult,
    CloseMode,
    InterruptOutcome,
    Outcome,
    ProbeResult,
    ReviewedSpec,
    RuntimeAttestation,
)
from acc.session_relay.adapters.codex import (
    ADAPTER_VERSION,
    NATIVE_SCHEMA,
    CodexAdapter,
    NativeRejection,
    StdioRPC,
)
from acc.session_relay.codex_proxy import CodexNativeProxy, CorrelatedRequest
from acc.session_relay.protocol import (
    Actor,
    ActorKind,
    Binding,
    CapabilityDeclaration,
    CapabilityEntry,
    Channel,
    ControlBinding,
    Fence,
    NativeAction,
    RelayError,
    Support,
    operation_digest,
)

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/session_relay/codex/native.json').read_text())
ACTOR = Actor(ActorKind.PERSON, 'person1', Channel.TERMINAL)
BINDING = Binding('binding1', 'install1', 'node1', 'account1', '/unused', 'codex', 'native1', 'sha256:' + '1' * 64)


class Owner:
    attestation = RuntimeAttestation('binding1', 'run1', 'native1', 'private-process1')
    def __init__(self):
        self.calls = []
        self.answer = FIXTURE['accepted']
        self.stopped = False
    async def request(self, method, params):
        self.calls.append((method, params))
        if method == 'thread/resume':
            return {'thread': {'id': 'native1'}}
        if method == 'initialize':
            return {}
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer
    async def notify(self, method, params):
        self.calls.append((method, params))
    async def stop(self):
        self.stopped = True


def operation(method='turn.submit', params=None, **changes):
    if params is None:
        params = {'text': 'hello'}
    op = AdapterOperation('op1', 'session1', 'run1', method, operation_digest(method, params), ACTOR,
                          Fence(1, 'broker1', 'hub1'), 2000, params)
    return replace(op, **changes)


async def adapter():
    owner = Owner()
    async def factory(spec):
        return owner
    async def probe(binding):
        return ProbeResult('fixture', 'sha256:' + '0' * 64, NATIVE_SCHEMA, 'stdio', True, frozenset({'text', 'interrupt'}))
    declaration = CapabilityDeclaration('codex', 'fixture', 'sha256:' + '0' * 64, ADAPTER_VERSION, NATIVE_SCHEMA,
        'stdio', 'isolated-fake-only', '2026-11-10', {'placement': 'test-fake'},
        {key: CapabilityEntry(Support.QUALIFIED, evidence=('synthetic fake only',)) for key in ('text', 'interrupt')})
    instance = CodexAdapter(factory, probe, declaration=declaration, clock=lambda: 1000)
    await instance.start(ReviewedSpec(BINDING, 'run1', {'session_id': 'session1'}))
    return instance, owner


async def test_submit_correlates_native_turn_and_interrupt_exact_same_run():
    instance, owner = await adapter()
    result = await instance.submit(operation())
    assert result.outcome is Outcome.ACCEPTED
    assert owner.calls[-1] == ('turn/start', FIXTURE['submit'])
    owner.answer = {}
    interrupt = operation('turn.interrupt', {'turnId': 'op1'}, operation_id='interrupt1')
    with pytest.raises(RelayError):
        await instance.interrupt(replace(interrupt, run_id='old-run'))
    result = await instance.interrupt(interrupt)
    assert result.outcome is InterruptOutcome.REQUESTED
    assert owner.calls[-1] == ('turn/interrupt', FIXTURE['interrupt'])
    await instance.close(CloseMode.DETACH)
    assert not owner.stopped
    await instance.close(CloseMode.STOP)
    assert owner.stopped


@pytest.mark.parametrize('response', [{}, {'turn': {'id': 'native-turn1'}}, ConnectionError('private vendor error')])
async def test_lost_or_unproven_admission_is_unknown_and_never_replayed(response):
    instance, owner = await adapter()
    owner.answer = response
    assert (await instance.submit(operation())).outcome is Outcome.UNKNOWN
    with pytest.raises(RelayError):
        await instance.submit(operation(operation_id='op2'))
    assert (await instance.reconcile(operation())).outcome is Outcome.UNKNOWN
    assert [call[0] for call in owner.calls].count('turn/start') == 1


async def test_correlated_rejection_and_unsupported_controls():
    instance, owner = await adapter()
    owner.answer = NativeRejection()
    assert (await instance.submit(operation())).outcome is Outcome.REJECTED
    with pytest.raises(RelayError):
        await instance.submit(operation(params={'text': 'hello', 'model': 'unreviewed'}))
    for method in (instance.steer, instance.reply_decision, instance.apply_next_turn):
        with pytest.raises(RelayError):
            await method(operation())
    proxy = await instance.native_client_proxy(BINDING)
    with pytest.raises(RelayError):
        await proxy.admit(NativeAction('request1', 1, 1))
    assert (await instance.read_snapshot()).state.value == 'unsupported'


async def test_unqualified_default_never_launches():
    calls = []
    async def factory(spec):
        calls.append(spec)
    instance = CodexAdapter(factory, None)
    assert all(item.status is Support.UNQUALIFIED for item in instance.declaration().operations.values())
    with pytest.raises(RelayError):
        await instance.start(ReviewedSpec(BINDING, 'run1', {}))
    assert not calls


async def test_native_delayed_request_keeps_original_actor_and_is_single_use():
    old = ControlBinding(ACTOR, 'attachment1', 1, 1, 1, 10)
    new = ControlBinding(replace(ACTOR, principal='person2'), 'attachment2', 2, 2, 1)
    calls = []
    async def admit(binding, method, params):
        calls.append((binding, method, params))
        return AdmissionResult(Outcome.UNKNOWN)
    proxy = CodexNativeProxy(lambda: (old, new), admit, correlation_qualified=True)
    action = NativeAction('request1', 1, 5)
    proxy.register_correlated(CorrelatedRequest(action, 'turn/start', {'text': 'hello'}))
    await proxy.admit(action)
    assert calls[0][0].actor.principal == 'person1'
    with pytest.raises(RelayError):
        await proxy.admit(action)
    for method in FIXTURE['forbidden_native']:
        with pytest.raises(RelayError):
            proxy.register_correlated(CorrelatedRequest(NativeAction('other', 1, 5), method, {}))
    with pytest.raises(RelayError):
        await proxy.admit(NativeAction('unregistered', 2, 1))
    assert len(calls) == 1


def test_real_guard_denies_fixture_command_without_executing_it():
    # Import only: never invoke bin/acc-* or its main(), read a live policy or run
    # a command. This is the actual guard's pure policy evaluator.
    path = Path(__file__).resolve().parents[1] / 'bin/acc-guard'
    loader = importlib.machinery.SourceFileLoader('relay_fixture_guard', str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    guard = importlib.util.module_from_spec(spec)
    loader.exec_module(guard)
    fixture = FIXTURE['guard_denial']
    denial = guard.check({'workspace': '/isolated-fixture', 'rules': {'force_push': True}},
                        fixture['tool'], fixture['args'], '/isolated-fixture')
    assert denial and denial[0] == fixture['rule']


async def test_stdio_lost_response_does_not_retry():
    reader = asyncio.StreamReader()
    class Writer:
        def __init__(self):
            self.writes = []
        def write(self, data):
            self.writes.append(data)
        async def drain(self):
            reader.feed_eof()
        def close(self):
            pass
    async def observe(message):
        raise AssertionError('no native event expected')
    writer = Writer()
    rpc = StdioRPC(reader, writer, observe)
    with pytest.raises(ConnectionError):
        await rpc.request('turn/start', FIXTURE['submit'])
    assert len(writer.writes) == 1
    await rpc.close()


def test_relay_launch_arguments_preserve_compiled_policy_and_force_hooks():
    from acc.providers.codex import PROVIDER
    from acc.server.compiler import LaunchInputs
    provider = PROVIDER
    inputs = LaunchInputs(provider='codex', sandbox='workspace-write', approval='on-request')
    args = provider.relay_args(inputs, files={})
    assert args[:3] == ['app-server', '--listen', 'stdio://']
    assert 'sandbox_mode="workspace-write"' in args
    assert 'approval_policy="on-request"' in args
    assert args[-2:] == ['-c', 'features.hooks=true']
    assert '--remote' not in args and 'resume' not in args


async def test_one_time_decision_reply_never_widens_policy_or_replays():
    instance, owner = await adapter()
    await instance.submit(operation())
    instance.record = replace(instance.record, operations={**instance.record.operations,
        'decision.tool_approval': CapabilityEntry(Support.QUALIFIED, evidence=('synthetic fake only',))})
    sent = []
    async def respond(request_id, result, *, before_write=None):
        if before_write is not None:
            await before_write()
        sent.append((request_id, result))
    owner.respond = respond
    instance.register_decision('decision1', native_request={'id': 42,
        'method': 'item/commandExecution/requestApproval', 'params': {
            'threadId': 'native1', 'turnId': 'native-turn1', 'itemId': 'item1'}},
        action_digest='sha256:' + '2' * 64, revision=1, expires_ms=2000)
    params = {'decisionId': 'decision1', 'choiceId': 'acceptForSession', 'expectedRevision': 1,
              'actionDigest': 'sha256:' + '2' * 64}
    with pytest.raises(RelayError):
        await instance.reply_decision(operation('decision.reply', params, operation_id='decision-op1'))
    assert not sent
    params = {**params, 'choiceId': 'decline'}
    op = operation('decision.reply', params, operation_id='decision-op1')
    assert (await instance.reply_decision(op)).outcome is Outcome.UNKNOWN
    with pytest.raises(RelayError):
        await instance.reply_decision(replace(op, operation_id='decision-op2'))
    assert sent == [(42, {'decision': 'decline'})]


async def test_main_registered_digest_survives_broker_and_native_projection(tmp_path):
    from relay_helpers import setup

    from acc.broker.session_relay import AdmissionBroker, AdmissionJournal
    from acc.server.session_relay.hub import Hub

    case = setup(tmp_path)
    journal = AdmissionJournal(tmp_path / 'journal', clock=lambda: 1000)
    try:
        instance, owner = await adapter()
        instance.session_id = 'relay'
        instance.attestation = replace(instance.attestation, run_id='1')
        params = {'sessionId': 'relay', 'expectedRunId': '1', 'text': 'hello',
                  'expectedConfigurationRevision': 0, 'ifBusy': 'queue'}
        digest = operation_digest('turn.submit', params)
        registration = Hub(case.ctx).register(case.resolve, 'relay', '1', 'public', 'turn.submit', digest)
        receipt = registration.receipt
        async def verify(op):
            return op.payload_digest == receipt.payload_digest and op.actor == receipt.actor
        broker = AdmissionBroker(journal, verify)
        fence = Fence(1, broker.incarnation, 'hub')
        await broker.enroll(fence, 'link')
        op = AdapterOperation(receipt.operation_id, 'relay', '1', 'turn.submit', digest,
                              receipt.actor, fence, 2000, params)
        assert (await broker.dispatch(op, instance.submit)).outcome is Outcome.ACCEPTED
        assert op.payload_digest == receipt.payload_digest
        assert op.actor == receipt.actor
        assert owner.calls[-1] == ('turn/start', FIXTURE['submit'])
        assert journal.lookup(op.operation_id)[0] == 'accepted'
    finally:
        journal.close()
        case.engine.dispose()


@pytest.mark.parametrize('answer', [NativeRejection(), ConnectionError(), TimeoutError(), {'unexpected': True}])
async def test_interrupt_failure_does_not_prove_native_turn_inactive_or_allow_replay(answer):
    instance, owner = await adapter()
    await instance.submit(operation())
    owner.answer = answer
    interrupt = operation('turn.interrupt', {'turnId': 'op1'}, operation_id='interrupt1')
    result = await instance.interrupt(interrupt)
    assert result.outcome is InterruptOutcome.UNKNOWN
    assert result.evidence is None and result.settles.value == 'uncertain'
    assert instance.active_turn == 'op1'
    with pytest.raises(RelayError):
        await instance.interrupt(interrupt)
    assert sum(method == 'turn/interrupt' for method, _ in owner.calls) == 1


async def test_interrupt_old_turn_never_cancels_replacement_turn():
    instance, owner = await adapter()
    await instance.submit(operation())
    # Trusted completion publication clears the active operation; a new
    # admitted operation has a separate native turn even within the same run.
    instance.active_turn = None
    owner.answer = {'turn': {'id': 'native-replacement', 'status': 'inProgress'}}
    await instance.submit(operation(operation_id='op2'))
    interrupt = operation('turn.interrupt', {'turnId': 'op1'}, operation_id='interrupt1')
    result = await instance.interrupt(interrupt)
    assert result.outcome is InterruptOutcome.NOT_ACTIVE
    assert result.turn_id == 'op1' and instance.active_turn == 'op2'
    assert not any(method == 'turn/interrupt' for method, _ in owner.calls)


@pytest.mark.parametrize('change', ['revoke', 'expire'])
async def test_interrupt_rechecks_authority_and_deadline_after_native_probe(change):
    instance, owner = await adapter()
    await instance.submit(operation())
    allowed = True
    original_probe = instance.probe

    async def probe(binding):
        nonlocal allowed
        result = await original_probe(binding)
        if change == 'revoke':
            allowed = False
        else:
            instance.clock = lambda: 2000
        return result

    async def authorize(op):
        return allowed

    instance.probe, instance.authorize = probe, authorize
    with pytest.raises(RelayError):
        await instance.interrupt(operation('turn.interrupt', {'turnId': 'op1'}, operation_id='interrupt1'))
    assert instance.active_turn == 'op1'
    assert not any(method == 'turn/interrupt' for method, _ in owner.calls)
