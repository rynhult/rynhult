"""Current-authorized, read-only retired-run evidence recovery; never replay."""
import asyncio
import secrets
import time
from types import SimpleNamespace

from acc.session_relay import protocol as p
from acc.session_relay.runtime_peer import RuntimePeer

from ..admin_features import require
from ..db import write_in_thread, write_intent
from ..ha import Fenced
from ..models import RelayRun, Session
from . import application_origins, application_settlement, authority, stop_proof, store


class ApplicationRecovery:
    def __init__(self, manager):
        self.manager = manager
        self.tasks = set()
        self.inflight = {}
        self.closing = False

    async def close(self):
        self.closing = True
        tasks = tuple(task for task in self.tasks if task is not asyncio.current_task())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def recover(self, resolve, identifier, operation_id):
        task = asyncio.current_task()
        actor_key = None
        peer = None
        token, deadline = secrets.token_hex(32), time.monotonic() + 10
        ctx = self.manager.ctx
        def scope(db):
            require(ctx.settings, 'session_relay')
            row, grant = authority.current(ctx, db, resolve, identifier)
            session = db.get(Session, identifier)
            operation = store.operation(db, identifier, operation_id, grant.actor)
            if session.provider != 'claude' or operation.receipt['method'] != 'decision.reply':
                return None
            if operation.receipt['state'] not in ('submitting', 'uncertain'):
                return None
            record = db.get(RelayRun, operation.run_id)
            if record is None:
                raise p.RelayError(p.ErrorCode.STALE_RUN)
            original = SimpleNamespace(ctx=ctx, session_id=identifier, run_id=record.run_id, binding_id=record.binding_id)
            params = application_origins.load_db(db, original, operation_id, retired=True)
            coordinates = store.coordinates(session)
            old_coordinates = record.identity['coordinates']
            if any(coordinates[key] != old_coordinates[key] for key in coordinates if key != 'nativeId'):
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            physical = stop_proof.placement(self.manager, db, session)
            old = record.identity['placement']
            # Fresh authenticated connection/main HA epoch may differ. The old
            # account/machine public identity and run directory may not.
            if physical[:2] != old[:2] or physical[2][:2] != old[2][:2] or physical[-1] != old[-1]:
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            expected = {'operationId': operation.id, 'sessionId': identifier, 'runId': str(operation.run_id),
                'method': 'decision.reply', 'payloadDigest': operation.receipt['payloadDigest'], 'actor': operation.receipt['actor'],
                'fence': {'authority_epoch': operation.authority['epoch'],
                    'broker_incarnation': record.identity['brokerIncarnation'], 'hub_incarnation': record.identity['hubIncarnation']}}
            return {'authority': grant.stamp(), 'placement': physical, 'params': params,
                'spec': {'binding': record.identity['binding'], 'directory': old[-1], 'operation': expected}}
        @write_intent
        def inspect():
            with ctx.db() as db:
                return scope(db)
        initial = None
        async def handle(method, params):
            if (method != 'authority.application_read' or params != {'grant': token}
                    or time.monotonic() >= deadline):
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            after = await write_in_thread(inspect)
            if initial is None or time.monotonic() >= deadline or p.canonical_json(after) != p.canonical_json(initial):
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            return initial['spec']
        @write_intent
        def commit(fact):
            with ctx.db() as db:
                after = scope(db)
                if time.monotonic() >= deadline or p.canonical_json(after) != p.canonical_json(initial):
                    raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
                if (not isinstance(fact, dict) or fact.keys() != {'version', 'binding', 'operation', 'application'}
                        or type(fact['version']) is not int or fact['version'] != 1
                        or p.canonical_json(fact['binding']) != p.canonical_json(initial['spec']['binding'])
                        or p.canonical_json(fact['operation']) != p.canonical_json(initial['spec']['operation'])):
                    raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
                return application_settlement.settle_db(ctx, db, identifier, initial['params'],
                    {'outcome': 'accepted', 'application': fact['application']}) == {'outcome': 'accepted'}
        try:
            initial = await write_in_thread(inspect)
            if initial is None or self.closing:
                return False
            key = p.canonical_json(initial['authority']['actor'])
            if self.inflight.get(key, 0) >= 16:
                return False
            # No await between admission and reservation; other actors cannot
            # spend this actor's budget. Only fresh authenticated scope supplies key.
            actor_key = key
            self.inflight[key] = self.inflight.get(key, 0) + 1
            self.tasks.add(task)
            host, account = initial['placement'][:2]
            client = ctx.broker.fresh(account, host)
            reader, writer = await asyncio.wait_for(client.session_runtime({'version': 4, 'enrollment': token}),
                max(.01, deadline - time.monotonic()))
            peer = RuntimePeer(reader, writer, handle, timeout=max(.01, deadline - time.monotonic()))
            fact = await peer.call('application.read', {})
            return await write_in_thread(lambda: commit(fact)) if fact is not None else False
        except (p.RelayError, Fenced, ConnectionError, OSError, TimeoutError, KeyError, ValueError):
            return False  # absent/unsupported/incomplete proof stays unknown, no fallback
        finally:
            if peer is not None:
                await peer.close()
            self.tasks.discard(task)
            if actor_key is not None:
                count = self.inflight[actor_key] - 1
                if count:
                    self.inflight[actor_key] = count
                else:
                    del self.inflight[actor_key]


def service(manager):
    if not hasattr(manager, 'application_recovery'):
        manager.application_recovery = ApplicationRecovery(manager)
    return manager.application_recovery
