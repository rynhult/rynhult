"""G6 actual runtime restart plus durable native outcome/expiry retention.

Main incarnations are fresh RuntimeManager/context objects with the same DB;
only the broker restart test claims an OS-process coordinator boundary.
"""
import asyncio
import copy
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from test_session_relay_native_lifetimes import cleaned, reserve
from test_session_relay_node_runtime import node_runtime  # noqa: F401
from test_session_relay_process_restart import process_node  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.broker.session_relay import AdmissionJournal
from acc.server.broker_client import Brokers
from acc.server.db import write_intent
from acc.server.models import RelayEvent, RelayOperation, RelayRun, RelaySession, utcnow
from acc.server.session_relay.hub import Hub
from acc.server.session_relay.maintenance import maintain, recover
from acc.server.session_relay.runtime import RuntimeManager
from acc.session_relay import protocol as p
from acc.session_relay.adapters.base import AdmissionResult, Outcome

pytestmark = pytest.mark.relay_native


@write_intent
def issue(c, launch, key):
    params = {'sessionId': launch.session_id, 'expectedRunId': str(launch.run_id), 'text': 'Lifetime fixture turn ' + key,
        'expectedControlRevision': 1, 'expectedConfigurationRevision': 0, 'ifBusy': 'reject'}
    with c.db() as db:
        db.get(RelaySession, launch.session_id).capabilities = {'text': 'qualified'}
        db.commit()
    registered = Hub(c.ctx).register(c.resolve, launch.session_id, str(launch.run_id), key,
        'turn.submit', p.operation_digest('turn.submit', params))
    token = launch.issue(c.resolve, registered.operation_id, params)
    return token, launch.grants[token].operation, params


async def submit(c, launch, key):
    token, operation, params = issue(c, launch, key)
    await launch.peer.call('stage', {'grant': token})
    hub = Hub(c.ctx)
    hub.queue(c.resolve, launch.session_id, operation.operation_id, expected_run=str(launch.run_id),
        digest=p.operation_digest('turn.submit', params))
    hub.claim(c.resolve, launch.session_id, operation.operation_id)
    receipt = await c.manager.dispatch(launch, token)
    for _ in range(500):
        if (await c.inspect('idle'))['idle']:
            break
        await asyncio.sleep(.02)
    else:
        raise AssertionError('Native turn did not complete')
    return operation, receipt


@pytest.mark.parametrize('incarnations', [2, pytest.param(129, marks=pytest.mark.relay_long_native)],
                         ids=['smoke', '129'])
async def test_native_runtime_restart_expiry_retention_and_persisted_fences(process_node, incarnations, record_property):  # noqa: F811
    c = process_node
    anchor = None
    main_ids = set()
    samples = []
    for epoch in range(1, incarnations + 1):
        launch = reserve(c, 10000 + epoch)
        await launch.launch(launch.session_id)
        main_ids.add(c.manager.incarnation)
        await c.inspect('qualify')
        if epoch == 1:
            accepted, receipt = await submit(c, launch, 'accepted')
            assert receipt.state is p.ReceiptState.ACCEPTED
            await c.inspect('uncertain')
            uncertain, receipt = await submit(c, launch, 'unknown')
            assert receipt.state is p.ReceiptState.UNCERTAIN
            anchor = (Path(c.factory.config.run_dir) / launch.session_id / 'relay/journal',
                accepted, uncertain, launch.session_id)
        # Expired current-main grants cannot stage or reach native IO.
        token, expired, _ = issue(c, launch, 'expired')
        launch.grants[token].operation = replace(expired, deadline_ms=1)
        with pytest.raises(ConnectionError):
            await launch.peer.call('stage', {'grant': token})
        await c.inspect('track')
        previous = c.manager
        previous_brokers = c.ctx.broker
        hub = previous_brokers.hub
        await previous.close()
        await cleaned(c)
        assert not c.wire.link._channels
        await asyncio.gather(*tuple(previous.background))
        # Recreate the main process-owned objects without restarting the broker.
        # Shared typed fixture wire survives; it supplies no authority renewal.
        previous_brokers.hub = None
        await previous_brokers.close()
        ctx = copy.copy(c.ctx)
        for name in ('_relay_recovery_done', '_relay_recovery_after', '_relay_retention_after'):
            ctx.__dict__.pop(name, None)
        ctx.broker = Brokers(c.factory.config.socket_path, None, previous_brokers.default_account,
            'fixture-services', hub=hub)
        c.manager = RuntimeManager(ctx)
        ctx.relay_manager = c.manager
        c.ctx = ctx
        recover(ctx, limit=incarnations + 2)
        maintain(ctx, limit=incarnations + 2)
        with c.db() as db:
            row = db.get(RelaySession, launch.session_id)
            assert row.sealed and row.capabilities == {}
            identity = db.get(RelayRun, launch.run_id).identity
            assert identity['hubIncarnation'] == previous.incarnation
        with pytest.raises((p.RelayError, ConnectionError)):
            launch.resolve_grant(token)
        # This fence was enrolled by the actual production BrokerRuntime, not
        # by the inspector. It persists across native cleanup/main replacement.
        current_root = Path(c.factory.config.run_dir) / launch.session_id / 'relay/journal'
        current_journal = AdmissionJournal(current_root)
        try:
            actual = json.loads(current_journal._meta('authority'))
            assert actual[:3] == [launch.fence.authority_epoch, launch.fence.broker_incarnation, previous.incarnation]
            with pytest.raises(p.RelayError):
                current_journal.enroll(replace(launch.fence, hub_incarnation=c.manager.incarnation), actual[3])
            with pytest.raises(p.RelayError):
                current_journal.claim(replace(expired, deadline_ms=1), actual[3])
        finally:
            current_journal.close()
        # Reopen the actual first native runtime's persisted journal, advance only
        # after a fresh authenticated native enrollment in this main lifetime.
        root, accepted, uncertain, anchor_sid = anchor
        now = max(accepted.deadline_ms, uncertain.deadline_ms) + epoch * 10000
        with_journal = AdmissionJournal(root, capacity=2, retention_ms=1, clock=lambda: now)
        try:
            persisted = json.loads(with_journal._meta('authority'))
            if epoch == 1:
                old_authority = persisted
                assert with_journal.lookup(accepted.operation_id)[0] == 'accepted'
                assert with_journal.lookup(uncertain.operation_id)[0] == 'uncertain'
            fence = p.Fence(epoch + 1, launch.fence.broker_incarnation, previous.incarnation)
            link = f'authenticated-lifetime-{epoch}'
            with_journal.enroll(fence, link)
            with pytest.raises(p.RelayError):
                with_journal.enroll(p.Fence(*old_authority[:3]), old_authority[3])
            with pytest.raises(p.RelayError):
                with_journal.enroll(replace(fence, hub_incarnation='stale-main'), link)
            with pytest.raises(p.RelayError):
                with_journal.claim(accepted, old_authority[3])
            fresh = replace(accepted, operation_id=f'retention-{epoch}', fence=fence,
                deadline_ms=now + 5)
            assert with_journal.claim(fresh, link)[0]
            # Reclaim a concluded row, keep the actual unresolved native outcome.
            with_journal.settle(fresh, AdmissionResult(Outcome.REJECTED,
                p.Evidence(p.EvidenceKind.BROKER_REFUSAL, fresh.operation_id, fresh.run_id)))
            with pytest.raises(p.RelayError) as full:
                with_journal.claim(replace(fresh, operation_id=f'capacity-{epoch}'), link)
            assert full.value.code is p.ErrorCode.LIMIT
            assert with_journal.lookup(uncertain.operation_id)[0] == 'uncertain'
            assert with_journal.db.execute('SELECT COUNT(*) FROM admission').fetchone()[0] == 2
            samples.append({'epoch': epoch, 'authority': json.loads(with_journal._meta('authority')),
                'rows': 2, 'main': previous.incarnation})
        finally:
            with_journal.close()
    assert len(main_ids) == incarnations
    assert (await c.inspect('state'))['requests'] == 2  # exactly the first accepted/unknown native turns
    @write_intent
    def retained():
        with c.db() as db:
            assert db.get(RelayOperation, uncertain.operation_id).receipt['state'] == 'uncertain'
            db.query(RelayEvent).filter(RelayEvent.session_id == anchor_sid).update(
                {RelayEvent.created_at: utcnow() - timedelta(days=31)})
            db.commit()
        maintain(c.ctx, limit=incarnations + 2)
        with c.db() as db:
            assert db.get(RelayOperation, uncertain.operation_id).receipt['state'] == 'uncertain'
            # Explicit deletion of a different ended enrollment reclaims its
            # tombstones, while the unresolved anchor enrollment is preserved.
            db.delete(db.get(RelaySession, launch.session_id))
            db.commit()
        with c.db() as db:
            assert db.get(RelayOperation, expired.operation_id) is None
            assert db.get(RelayOperation, uncertain.operation_id).receipt['state'] == 'uncertain'
    retained()
    with pytest.raises(p.RelayError):
        launch.resolve_grant(token)
    replacement = reserve(c, 10001 + incarnations)
    await replacement.launch(replacement.session_id)
    await c.inspect('track')
    await replacement.peer.close()
    await cleaned(c)
    await asyncio.gather(*tuple(c.manager.background))
    assert not c.manager.runs

    evidence = {'incarnations': incarnations, 'requests': 2, 'samples': samples,
        'scope': 'native runtime teardown/fresh enrollment; actual outcome retention; simulated main object restart and epoch enrollment; no OS-main/HA claim'}
    Path(f'/tmp/relay-g56-runtime-retention-{incarnations}.json').write_text(json.dumps(evidence, indent=2) + '\n')
    record_property('runtime_retention', json.dumps(evidence))
