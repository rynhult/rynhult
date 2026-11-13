"""Real public cursor/attachment recovery with an already-owned native provider."""
from datetime import timedelta

from test_session_relay_public_writer import attach, call

from acc.server.db import write_intent
from acc.server.models import RelayOperation, RelaySession, utcnow
from acc.server.session_relay import history, store
from acc.session_relay import protocol as p


async def qualify_history_fault(c, client, sid, fault):
    attached = await attach(client, 'native-history-before-' + fault, ['queue'])
    snapshot = (await call(client, 'session.snapshot', {}))['result']
    page = await call(client, 'events.read', {'attachmentId': attached['attachmentId'], 'limit': 1})
    assert 'result' in page, page
    cursor = page['result']['cursor']
    with c.db() as db:
        original = {row.id: dict(row.receipt) for row in db.query(RelayOperation).filter(RelayOperation.session_id == sid)}
        old_epoch = db.get(RelaySession, sid).history_epoch
    @write_intent
    def mutate():
        with c.db() as db:
            row = db.get(RelaySession, sid)
            if fault == 'epoch':
                history.reset(db, row)
            else:
                store.prune(db, row, before=utcnow() + timedelta(seconds=1))
                assert row.floor_seq > 1
            db.commit()
    mutate()
    refused = await call(client, 'events.read', {'attachmentId': attached['attachmentId'], 'cursor': cursor, 'limit': 10})
    assert 'error' in refused, refused
    if fault == 'epoch':
        assert 'error' in await call(client, 'session.snapshot', {})
        params = {'sessionId': sid, 'expectedRunId': str(c.launch.run_id),
            'text': 'Never replay a stale history attachment',
            'expectedControlRevision': snapshot['controlRevision'],
            'expectedConfigurationRevision': 0, 'ifBusy': 'reject'}
        registration = await call(client, 'operation.register', {'expectedRunId': str(c.launch.run_id),
            'registrationKey': 'stale-epoch-input', 'method': 'turn.submit',
            'payloadDigest': p.operation_digest('turn.submit', params)})
        assert 'error' in registration, registration
    fresh = await attach(client, 'native-history-after-' + fault, ['queue'])
    recovered = await call(client, 'events.read', {'attachmentId': fresh['attachmentId'], 'limit': 10})
    assert 'result' in recovered, recovered
    assert recovered['result']['historyEpoch'] == old_epoch + (fault == 'epoch')
    if fault == 'compaction':
        assert recovered['result']['gap']
    with c.db() as db:
        assert all(db.get(RelayOperation, identifier).receipt == receipt for identifier, receipt in original.items())
        assert db.get(RelaySession, sid).current_run_id == c.launch.run_id
    assert fresh['attachmentId']
