"""Abrupt-exit worker; only parent-owned fixture DB/journal and fake provider."""
import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

from acc.broker.session_relay import AdmissionBroker, AdmissionJournal
from acc.server.api.context import Context, Principal
from acc.server.config import Settings
from acc.server.db import make_engine, make_sessionmaker, write_intent
from acc.server.models import RelayOperation
from acc.server.session_relay import store
from acc.server.session_relay.hub import Hub
from acc.session_relay import protocol as p
from acc.session_relay.adapters.base import AdapterOperation, AdmissionResult, Outcome


async def main():
    root, phase = Path(sys.argv[1]), sys.argv[2]
    assert root.is_absolute() and str(root).startswith('/tmp/')
    ctx = SimpleNamespace(db=make_sessionmaker(make_engine(f'sqlite:///{root}/relay.db')),
                          settings=Settings(data_dir=root / 'data', feature_overrides={'session_relay': True}))
    ctx.session_for = lambda db, who, identifier: Context.session_for(ctx, db, who, identifier)
    ctx.owns = Context.owns
    actor = p.Actor(p.ActorKind.PERSON, 'alex', p.Channel.CHAT)
    def resolve():
        return Principal('alex', 'owner', 'fixture', 1), actor
    def crash(at):
        if phase == at:
            os._exit(73)  # actual process death; no finally/cleanup or simulated return
    hub = Hub(ctx)
    params = {'sessionId': 'relay', 'expectedRunId': '1', 'text': 'Artificial crash fixture',
              'expectedConfigurationRevision': 0, 'ifBusy': 'reject'}
    digest = p.operation_digest('turn.submit', params)
    crash('before_registration')
    registered = hub.register(resolve, 'relay', '1', 'crash-fixture', 'turn.submit', digest)
    crash('after_registration')
    hub.queue(resolve, 'relay', registered.operation_id, expected_run='1', digest=digest)
    hub.claim(resolve, 'relay', registered.operation_id)
    crash('after_main_claim')
    journal = AdmissionJournal(root / 'journal', clock=lambda: 1000)
    async def verify(_):
        return True
    broker = AdmissionBroker(journal, verify)
    fence = p.Fence(1, broker.incarnation, 'fixture-hub')
    await broker.enroll(fence, 'fixture-link')
    operation = AdapterOperation(registered.operation_id, 'relay', '1', 'turn.submit', digest, actor, fence, 2000, params)
    async def send(op):
        crash('after_broker_claim')
        with open(root / 'provider-admissions', 'ab', buffering=0) as stream:
            stream.write((op.operation_id + '\n').encode())
            os.fsync(stream.fileno())
        crash('after_provider_admission')
        return AdmissionResult(Outcome.ACCEPTED,
            p.Evidence(p.EvidenceKind.PROVIDER_ADMISSION, op.operation_id, op.run_id, 'fixture-provider'), 'fixture-turn')
    result = await broker.dispatch(operation, send)
    crash('after_acknowledgement')
    @write_intent
    def settle():
        with ctx.db() as db:
            store.settle(db, store.lock(db, 'relay'), db.get(RelayOperation, registered.operation_id),
                         evidence=result.evidence, accepted=True)
            db.commit()
    settle()
    crash('after_receipt_commit')
    raise AssertionError('Unknown crash phase')


asyncio.run(main())
