"""Production application-observer scheduler pressure; synthetic hook identities.

This is scheduler qualification with an owned fake CLI, not seventeen native
permission callbacks or provider-application evidence.
"""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

from test_session_relay_claude_agent import until
from test_session_relay_claude_public import mutate, public_claude  # noqa: F401
from test_session_relay_claude_public_decisions import pending_hook, qualify
from test_session_relay_ownership import ownership_case  # noqa: F401
from test_session_relay_public_writer import attach
from test_session_relay_runtime import runtime_case  # noqa: F401


async def test_application_task_overflow_release_and_owner_retirement(public_claude, monkeypatch):  # noqa: F811
    c = public_claude
    qualify(c, monkeypatch)
    monkeypatch.setattr('acc.broker.relay_ownership.APPLICATION_OBSERVATION_SECONDS', 0)
    await attach(c.client, 'application-pressure', ['decisions'])
    pending, decision = await pending_hook(c)
    response, _ = await mutate(c, 'decision.reply', {'decisionId': decision['decisionId'],
        'choiceId': 'decline', 'expectedRevision': decision['revision'],
        'actionDigest': decision['actionDigest']}, 'one-original-capacity-answer')
    assert response['result'] == {'outcome': 'unknown'}
    await pending
    broker = c.broker_state
    await until(lambda: not broker.application_tasks)
    value, owner, _ = next(iter(broker.application_records.values()))
    broker.application_records.clear()
    entered, releases, publications = [], {}, []
    class Source:
        async def inspect_application(self, *_args, **_kwargs):
            task = asyncio.current_task()
            releases[task] = asyncio.Event()
            entered.append(task)
            await releases[task].wait()
            return {'outcome': 'unknown'}
    async def forbidden_publication(payload):
        publications.append(payload)
        raise AssertionError('Unknown observation cannot publish evidence')
    broker.publish = forbidden_publication
    source = Source()
    for i in range(17):
        identifier = f'capacity-{i:032d}'
        entry = deepcopy(value)
        entry['operation']['operationId'] = identifier
        broker.application_records[identifier] = (entry, owner, source)
        broker.start_application_observer(identifier, once=True)
    await until(lambda: len(entered) == 16)
    assert len(broker.application_tasks) == len(broker.application_inflight) == 16
    overflow = identifier
    assert overflow not in broker.application_inflight
    releases[entered[0]].set()
    await until(lambda: len(broker.application_tasks) == 15)
    broker.start_application_observer(overflow, once=True)
    await until(lambda: len(entered) == 17)
    assert len(broker.application_tasks) == 16 and overflow in broker.application_inflight
    for release in releases.values():
        release.set()
    await until(lambda: not broker.application_tasks)
    assert not broker.application_inflight and len(broker.application_records) == 17
    broker.owners.current = SimpleNamespace()
    broker.prune_applications()
    broker.owners.current = owner
    assert not broker.application_records and not publications


async def test_security_s3_recovery_capacity_is_per_authenticated_actor(monkeypatch):
    from acc.server.session_relay import application_recovery as module
    from acc.session_relay import protocol as p
    calls = []
    def fresh(*args):
        calls.append(args)
        raise ConnectionError('fixture no broker IO')
    service = module.ApplicationRecovery(SimpleNamespace(ctx=SimpleNamespace(broker=SimpleNamespace(fresh=fresh))))
    service.tasks.update(object() for _ in range(16))
    alice = p.to_wire(p.Actor(p.ActorKind.PERSON, 'alice', p.Channel.CHAT))
    bob = p.to_wire(p.Actor(p.ActorKind.PERSON, 'bob', p.Channel.CHAT))
    service.inflight = {p.canonical_json(alice): 16}
    current = [bob]
    async def inspect(work):
        assert work.__name__ == 'inspect'
        return {'authority': {'actor': current[0]}, 'placement': ('local', 'fixture')}
    monkeypatch.setattr(module, 'write_in_thread', inspect)
    assert await service.recover(None, 'fixture', 'operation') is False
    assert calls == [('fixture', 'local')]
    current[0] = alice
    assert await service.recover(None, 'fixture', 'operation') is False
    assert len(calls) == 1
    assert service.inflight == {p.canonical_json(alice): 16}
