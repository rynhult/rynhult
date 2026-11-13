"""Observe an existing interrupt journal result; never stage or resend input."""
import asyncio
import secrets
import time
from dataclasses import dataclass

from acc.session_relay import protocol as p

from ..db import write_intent
from ..models import AuditLog, Session
from . import authority, store


@dataclass
class Observation:
    resolve: object
    stamp: object
    identity: dict
    expires: float


class ReceiptReads:
    def __init__(self, run):
        self.run, self.grants, self.inflight = run, {}, set()

    def stored(self, resolve, identifier):
        with self.run.ctx.db() as db:
            _, current = authority.current(self.run.ctx, db, resolve, self.run.session_id)
            return store.receipt_from(store.operation(db, self.run.session_id, identifier, current.actor).receipt)

    def current(self, resolve, identifier, db=None):
        if db is None:
            with self.run.ctx.db() as db:
                return self.current(resolve, identifier, db)
        run = self.run
        row, current = authority.current(run.ctx, db, resolve, run.session_id, expected_run=run.run_id)
        session = db.get(Session, run.session_id)
        if (row.sealed or row.binding_id != run.binding_id or row.authority_epoch != run.fence.authority_epoch
                or store.coordinates(session) != run.coordinates
                or run.manager.current_placement(db, session, provider=run.provider) != run.placement):
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        record = store.operation(db, run.session_id, identifier, current.actor)
        receipt = store.receipt_from(record.receipt)
        if receipt.method != 'turn.interrupt' or receipt.run_id != str(run.run_id):
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        identity = {'operationId': identifier, 'sessionId': run.session_id, 'runId': str(run.run_id),
            'method': receipt.method, 'payloadDigest': receipt.payload_digest, 'actor': p.to_wire(receipt.actor),
            'fence': p.to_wire(run.fence)}
        return (row.enrollment, row.history_epoch, current), identity

    def verify(self, token, db=None):
        grant = self.grants.get(token)
        if grant is None or grant.expires <= time.monotonic():
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        stamp, identity = self.current(grant.resolve, grant.identity['operationId'], db)
        if (stamp != grant.stamp or identity != grant.identity or self.grants.get(token) is not grant
                or grant.expires <= time.monotonic()):
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        return {'identity': identity, 'bindingId': self.run.binding_id}

    @write_intent
    def settle(self, token, result):
        with self.run.ctx.db() as db:
            row = store.lock(db, self.run.session_id)
            self.verify(token, db)
            grant = self.grants[token]
            row, current = authority.current(self.run.ctx, db, grant.resolve, self.run.session_id,
                expected_run=self.run.run_id, action='read')
            row = store.lock(db, row.session_id)
            # Repeat after the row lock: another observer/Stop may have won it.
            self.verify(token, db)
            record = store.operation(db, row.session_id, grant.identity['operationId'], current.actor)
            if record.receipt['state'] != 'uncertain':
                return store.receipt_from(record.receipt)
            if not isinstance(result, dict) or result.keys() != {'outcome', 'evidence'}:
                raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            if result['outcome'] == 'unknown' and result['evidence'] is None:
                return store.receipt_from(record.receipt)
            if result['outcome'] not in ('accepted', 'rejected') or not isinstance(result['evidence'], dict):
                raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            proof = result['evidence']
            if (not {'kind', 'operationId', 'runId'} <= proof.keys()
                    or proof.keys() - {'kind', 'operationId', 'runId', 'nativeRef'}
                    or (proof.get('nativeRef') is not None and not p.valid_opaque(proof['nativeRef']))):
                raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            try:
                evidence = p.Evidence(p.EvidenceKind(proof['kind']), proof['operationId'], proof['runId'], proof.get('nativeRef'))
            except (KeyError, ValueError, TypeError):
                raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE) from None
            if (evidence.operation_id, evidence.run_id) != (record.id, str(record.run_id)):
                raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            accepted = result['outcome'] == 'accepted'
            if evidence.kind not in ({p.EvidenceKind.PROVIDER_ADMISSION} if accepted else
                    {p.EvidenceKind.PROVIDER_REJECTION, p.EvidenceKind.BROKER_REFUSAL}):
                raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            receipt = store.settle(db, row, record, evidence=evidence, accepted=accepted)
            db.add(AuditLog(actor=current.actor.principal, action='relay.receipt.reconciled', target=row.session_id,
                detail={'operation_id': record.id, 'run_id': record.run_id, 'method': receipt.method, 'state': receipt.state.value}))
            db.commit()
            return receipt

    async def read(self, resolve, identifier):
        receipt = await asyncio.to_thread(self.stored, resolve, identifier)
        if receipt.state is not p.ReceiptState.UNCERTAIN or receipt.method != 'turn.interrupt' or self.run.peer.closed:
            return receipt
        if len(self.grants) >= 16 or len(self.inflight) >= 16:
            raise p.RelayError(p.ErrorCode.LIMIT)
        stamp, identity = await asyncio.to_thread(self.current, resolve, identifier)
        if len(self.grants) >= 16 or len(self.inflight) >= 16:
            raise p.RelayError(p.ErrorCode.LIMIT)
        token = secrets.token_hex(32)
        self.grants[token] = Observation(resolve, stamp, identity, time.monotonic() + 10)
        try:
            task = asyncio.create_task(self.run.peer.call('receipt.read', {'grant': token}))
            self.inflight.add(task)
            def done(future):
                self.inflight.discard(future)
                if not future.cancelled():
                    future.exception()
            task.add_done_callback(done)
            result = await asyncio.shield(task)
            from ..db import write_in_thread
            return await write_in_thread(lambda: self.settle(token, result))
        except (ConnectionError, OSError, TimeoutError):
            return await asyncio.to_thread(self.stored, resolve, identifier)
        finally:
            self.grants.pop(token, None)


def service(run):
    if not hasattr(run, 'receipt_reads'):
        run.receipt_reads = ReceiptReads(run)
    return run.receipt_reads
