"""Main authority for reviewed new-session launches and opaque broker grants.

All tokens are connection-bound server-held references, never serialized actor
credentials. Every callback opens a fresh transaction; no DB snapshot crosses IO.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, replace

from sqlalchemy import select

from acc.broker import ops
from acc.broker.relay_runtime import operation_from, operation_wire
from acc.session_relay import protocol as p
from acc.session_relay.adapters.base import AdapterOperation
from acc.session_relay.approval_facts import matches_action
from acc.session_relay.runtime_peer import RuntimePeer

from .. import budgets, security_settings
from ..db import write_in_thread, write_intent
from ..models import AuditLog, Node, RelayOwnership, RelayRun, RelaySession, Session, SessionRun, utcnow
from . import authority, history, store
from .hub import Hub


class RequestResolver:
    def __init__(self, ctx, request, attached=None, *, attachment_id=None):
        self.ctx, self.request, self.attached = ctx, request, attached
        self.attachment_id = attachment_id

    def __call__(self):
        with self.ctx.db() as db:
            return self.in_transaction(db)

    def authenticate_in_transaction(self, db):
        # Only the synchronous main-frame context enables this map. Reuse a
        # successful login for this exact resolver/transaction; attachment,
        # resource, policy, run and writer checks still execute at each caller.
        # No result survives the context or the next post-await DB transaction.
        principals = db.info.get('relay_frame_principals')
        key = (self, db.get_transaction())
        if principals is not None and key in principals:
            return principals[key]
        who = self.ctx.relay_principal(db, self.request)
        actor = authority.authenticated_actor(who, cookie_authenticated=True,
            native=getattr(self.request.state, 'edge_native', False),
            device=getattr(self.request.state, 'edge_fingerprint', None))
        transaction = db.get_transaction()
        if principals is not None and transaction is not None:
            principals[self, transaction] = (who, actor)
        return who, actor

    def in_transaction(self, db):
        who, actor = self.authenticate_in_transaction(db)
        if self.attached:
            self.attached(db, actor)
        return who, actor


@dataclass
class Grant:
    operation: AdapterOperation
    resolve: object
    authority: object
    staging: dict | None = None
    native_decision: object | None = None
    native_interrupt: object | None = None


class RuntimeManager:
    def __init__(self, ctx):
        self.ctx = ctx
        self.incarnation = uuid.uuid4().hex
        self.launches = {}
        self.runs = {}
        self.background = set()
        ctx.broker.runtime_managers.add(self)

    def is_current_launch(self, identifier):
        return identifier in self.launches or identifier in self.runs

    def new_launch(self, resolve):
        return Launch(self, resolve)

    def new_exclusive_launch(self, resolve):
        # Internal reviewed-launch entry only. Public enrollment/capabilities
        # remain unchanged until native ownership qualification is approved.
        from .exclusive_runtime import ExclusiveLaunch
        return ExclusiveLaunch(self, resolve)

    async def recover_application(self, resolve, session_id, operation_id):
        from .application_recovery import service
        return await service(self).recover(resolve, session_id, operation_id)

    async def reconcile_decisions(self, session_id, run_id):
        from .reconciliation import OriginalEvidence
        return await OriginalEvidence(self, session_id, run_id).reconcile()

    async def read_terminal(self, resolve, session_id, params):
        from .terminal_read import service
        run = self.runs.get(session_id)
        if run is None:
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        return await service(run).read(resolve, params['expectedRunId'])

    async def claim_terminal(self, resolve, session_id, params):
        run = self.runs.get(session_id)
        if run is None or run.provider not in ('codex', 'claude'):
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        return await run.terminal_grants.claim(resolve, params)

    async def input_terminal(self, resolve, session_id, params):
        import base64
        run = self.runs.get(session_id)
        if run is None or run.provider not in ('codex', 'claude'):
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        return await run.terminal_grants.input(resolve, params, base64.b64decode(params['data'], validate=True))

    async def drain_terminal(self, resolve, session_id, params):
        run = self.runs.get(session_id)
        if run is None or run.provider not in ('codex', 'claude'):
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        return await write_in_thread(lambda: run.terminal_grants.watermark(resolve, params))

    async def take_writer(self, resolve, session_id, params):
        from .writers import take
        run = self.runs.get(session_id)
        if run is None or run.provider != 'codex':
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        return await take(run, resolve, params)

    async def answer_decision(self, resolve, session_id, params):
        from .native_decisions import answer
        run = self.runs.get(session_id)
        if run is not None and run.provider == 'claude':
            from .claude_public import PUBLIC_CLAUDE_DECISIONS_QUALIFIED
            if PUBLIC_CLAUDE_DECISIONS_QUALIFIED:
                from .claude_public import answer as claude_answer
            else:
                from .claude_decisions import answer as claude_answer
            return await claude_answer(run, resolve, params)
        if run is None or run.provider != 'codex':
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        return await answer(run, resolve, params)

    async def observe_operation(self, resolve, session_id, identifier):
        def stored():
            with self.ctx.db() as db:
                _, current = authority.current(self.ctx, db, resolve, session_id)
                return store.receipt_from(store.operation(db, session_id, identifier, current.actor).receipt)
        receipt = await asyncio.to_thread(stored)
        run = self.runs.get(session_id)
        if (run is None or run.provider not in ('codex', 'claude') or receipt.run_id != str(run.run_id)
                or receipt.method != 'turn.interrupt' or receipt.state is not p.ReceiptState.UNCERTAIN):
            return receipt
        from .receipt_read import service
        try:
            return await service(run).read(resolve, identifier)
        except p.RelayError:
            return await asyncio.to_thread(stored)

    async def interrupt(self, resolve, session_id, params):
        from .interrupts import interrupt
        run = self.runs.get(session_id)
        if run is None or run.provider not in ('codex', 'claude'):
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        return await interrupt(run, resolve, params)

    async def select_owner(self, resolve, session_id, *, target, revision, request_key):
        from .exclusive_runtime import ExclusiveLaunch
        run = self.runs.get(session_id)
        if not isinstance(run, ExclusiveLaunch):
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        return await run.select_owner(resolve, target=target, revision=revision, request_key=request_key)

    def current_placement(self, db, session, *, provider='codex', evidence_only=False):
        broker = self.ctx.broker
        leader = getattr(broker, 'authority', None)
        if leader:
            leader.check()
        host = session.host or 'local'
        if host != 'local':
            node = db.scalar(select(Node).where(Node.name == host))
            if node is None or node.status != 'active':
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            link = broker.hub.get(host)
            if link.closed or session.run_as not in link.accounts:
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            node_identity = [node.id, node.public_key, id(link)]
        else:
            if leader:
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            node_identity = ['local']
        if (session.container or session.management != 'acc' or session.provider != provider
                or session.multirun_id or session.automation):
            # Service/assignment modes stay refused until their specific grants qualify.
            raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
        team_grant = None
        if session.team_id and not evidence_only:
            from .delivery import CLAUDE_SERVICE_QUALIFIED, DELIVERY_QUALIFIED, team_scope
            if (DELIVERY_QUALIFIED is not True or provider not in ('codex', 'claude')
                    or provider == 'claude' and (CLAUDE_SERVICE_QUALIFIED is not True or session.claude_auth != 'api_key')):
                raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
            team_grant = team_scope(db, session)
        if not evidence_only:
            policy = security_settings.resolve(db, owner=session.owner, project_id=session.project_id,
                                               machine=host, account=session.run_as)
            if budgets.paused(db, session.owner, session.provider, project_id=session.project_id,
                              machine=host, account=session.run_as, policy=policy):
                raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
            security_settings.check_generation(db, session, current=policy)
            from acc.backup_activation import held

            from .. import sources
            if held(db, session) or sources.session_problem(db, session):
                raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
        return [host, session.run_as, node_identity, getattr(leader, 'epoch', None),
                getattr(leader, 'incarnation', None), session.run_dir,
                *([{'team': team_grant}] if team_grant is not None else [])]

    async def submit(self, resolve, session_id, params):
        run = self.runs.get(session_id)
        if run is None:
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        async with run.submit_lock:
            return await self._submit(resolve, session_id, params)

    async def _submit(self, resolve, session_id, params):
        run = self.runs.get(session_id)
        if run is None or run.peer.closed:
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        identifier = params['operationId']
        payload = {key: value for key, value in params.items() if key != 'operationId'}
        token = await write_in_thread(lambda: run.issue(resolve, identifier, payload))
        if not isinstance(token, str):
            return token  # an existing receipt, never permission to resend
        try:
            await run.peer.call('stage', {'grant': token})
            await write_in_thread(lambda: run.resolve_grant(token))
            hub = Hub(self.ctx)
            receipt = await write_in_thread(lambda: hub.queue(resolve, session_id, identifier,
                expected_run=params['expectedRunId'], digest=p.operation_digest('turn.submit', payload),
                if_busy=params.get('ifBusy', 'reject')))
            try:
                receipt = await write_in_thread(lambda: hub.claim(resolve, session_id, identifier))
            except p.RelayError as error:
                if error.code is p.ErrorCode.BUSY:
                    return receipt
                raise
        except Exception:
            await write_in_thread(lambda: run.fail_queued(token))
            await self.discard(run, token)
            raise
        return await self.dispatch(run, token)

    async def dispatch(self, run, token):
        try:
            await write_in_thread(lambda: run.resolve_grant(token))
            result = await run.peer.call('dispatch', {'grant': token})
        except Exception:
            result = {'outcome': 'unknown'}
        # Late conclusive evidence settles the original receipt even if authority
        # ended after provider admission. It grants no new execution authority.
        return await write_in_thread(lambda: run.settle(token, result))

    async def cancel(self, resolve, session_id, params):
        async def cancel_queued(run=None):
            # Authorization and receipt CAS precede any broker/resource effect.
            receipt = await write_in_thread(lambda: Hub(self.ctx).cancel(resolve, session_id,
                params['operationId'], params['expectedRevision']))
            if run is not None:
                for token, grant in list(run.grants.items()):
                    if grant.operation.operation_id == params['operationId']:
                        await self.discard(run, token)
            return receipt

        run = self.runs.get(session_id)
        if run is None:
            return await cancel_queued()
        @write_intent
        def already_claimed():
            from fastapi import HTTPException
            try:
                with self.ctx.db() as db:
                    _, current = authority.current(self.ctx, db, resolve, session_id, action='operation.cancel')
                    operation = store.operation(db, session_id, params['operationId'], current.actor)
                    delivery_scope = getattr(current, 'delivery_scope', None)
                    if delivery_scope is not None:
                        from .delivery import check_operation
                        check_operation(delivery_scope, operation)
                    return store.receipt_from(operation.receipt).state is not p.ReceiptState.QUEUED
            except (p.RelayError, HTTPException):
                return False  # ordinary Hub path reauthenticates and audits the refusal

        if run.submit_lock.locked() and await asyncio.to_thread(already_claimed):
            # A claimed/terminal receipt never returns to QUEUED. Hub.cancel
            # reauthorizes and refuses its CAS before any discard/resource effect;
            # do not wait for that operation's provider IO to release submit_lock.
            return await cancel_queued()
        # A queued operation cannot be claimed while its cancellation commits
        # and its already-failed private stage is reclaimed.
        async with run.submit_lock:
            return await cancel_queued(run)

    async def drain(self, run):
        async with run.submit_lock:
            await self._drain(run)

    async def _drain(self, run):
        hub = Hub(self.ctx)
        for token, grant in list(run.grants.items()):
            if grant.operation.method != 'turn.submit':
                continue  # decision dispatch belongs to its single CAS/answer task
            if not run.terminal_grants.queued_dispatch_allowed(token):
                continue
            try:
                receipt = await write_in_thread(lambda: hub.claim(grant.resolve, run.session_id,
                                                  grant.operation.operation_id))
                if receipt.state is p.ReceiptState.SUBMITTING:
                    await self.dispatch(run, token)
                    return
                if receipt.state is p.ReceiptState.FAILED:
                    await self.discard(run, token)
            except p.RelayError as error:
                if error.code is p.ErrorCode.BUSY:
                    return
                await write_in_thread(lambda: run.fail_queued(token))
                await self.discard(run, token)

    async def discard(self, run, token):
        try:
            await run.peer.call('discard', {'grant': token})
        except ConnectionError:
            pass  # A disconnected broker retains the private bytes for recovery.
        finally:
            run.grants.pop(token, None)

    async def stop(self, identifier):
        from . import stop_proof
        run = self.runs.get(identifier)
        if run is None:
            return await write_in_thread(lambda: stop_proof.verified(self, identifier))
        try:
            result = await run.peer.call('stop', {})
            expected = {'exited'} if run.provider == 'claude' else {'exited', 'wholeTree'}
            if (not isinstance(result, dict) or result.keys() != expected or result['exited'] is not True
                    or (run.provider == 'codex' and result['wholeTree'] != 'enclosure-v1')):
                return False
            try:
                return await write_in_thread(lambda: stop_proof.remember(self, run))
            except p.RelayError:
                return False  # the old exit cannot confirm a changed run/placement
        except (ConnectionError, OSError, asyncio.TimeoutError):
            return False
        finally:
            await run.peer.close()
            if self.runs.get(identifier) is run:
                self.runs.pop(identifier, None)

    async def close(self):
        recovery = getattr(self, 'application_recovery', None)
        if recovery is not None:
            await recovery.close()
        tasks = [run.drain_task for run in self.runs.values() if run.drain_task and not run.drain_task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        peers = {run.peer for run in (*self.runs.values(), *self.launches.values()) if run.peer}
        await asyncio.gather(*(peer.close() for peer in peers), return_exceptions=True)
        await asyncio.gather(*self.background, return_exceptions=True)
        self.runs.clear()
        self.launches.clear()



class Launch:
    provider = 'codex'
    protocol_version = 2
    def __init__(self, manager, resolve):
        self.manager, self.ctx, self.resolve = manager, manager.ctx, resolve
        self.token = secrets.token_hex(32)
        self.created = time.monotonic()
        self.peer = None
        self.grants = {}
        from .terminal_grants import TerminalGrants
        self.terminal_grants = TerminalGrants(self)
        self.fence = None
        self.drain_task = None
        self.submit_lock = asyncio.Lock()
        self.binding = None
        self.placement = None
        self.coordinates = None
        self.policy_revision = None
        self.session_id = self.run_id = None

    def reserve(self, db, session):
        try:
            return self._reserve(db, session)
        except (p.RelayError, security_settings.Conflict) as error:
            from ..supervisor_common import LaunchRefused
            raise LaunchRefused(str(error)) from None

    def _reserve(self, db, session):
        from ..admin_features import require
        require(self.ctx.settings, 'session_relay')
        who, actor = self.resolve.in_transaction(db) if hasattr(self.resolve, 'in_transaction') else self.resolve()
        if actor.kind is not p.ActorKind.PERSON or who.user_id is None or session.owner != actor.principal:
            raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
        from .release_scope import local
        local(session, actor)
        self.placement = self.manager.current_placement(db, session, provider=self.provider)
        self.coordinates = store.coordinates(session)
        self.policy_revision = security_settings.revision(db)
        self.session_id = session.id
        self.binding_id = uuid.uuid4().hex
        run = SessionRun(session_id=session.id, reason='relay_launch')
        db.add(run)
        db.flush()
        self.run_id = run.id
        db.add(RelaySession(session_id=session.id, enrollment=uuid.uuid4().hex,
                            current_run_id=run.id, binding_id=self.binding_id))
        db.flush()
        self.manager.launches[session.id] = self

    def check_launch(self, db):
        from ..admin_features import require
        require(self.ctx.settings, 'session_relay')
        who, actor = self.resolve.in_transaction(db) if hasattr(self.resolve, 'in_transaction') else self.resolve()
        session = self.ctx.session_for(db, who, self.session_id)
        row = store.lock(db, self.session_id)
        native = db.get(SessionRun, self.run_id)
        if (actor.kind is not p.ActorKind.PERSON or actor.principal != session.owner
                or not native or native.ended_at or session.desired_state != 'running'
                or row.sealed or row.current_run_id != self.run_id or row.binding_id != self.binding_id
                or row.authority_epoch != 1 or security_settings.revision(db) != self.policy_revision
                or self.manager.current_placement(db, session, provider=self.provider) != self.placement
                or store.coordinates(session) != self.coordinates):
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        return session, row

    @write_intent
    def enrollment(self, params):
        if (params.keys() != {'enrollment', 'linkId'} or params['enrollment'] != self.token
                or time.monotonic() - self.created > 60 or self.binding is not None):
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        with self.ctx.db() as db:
            session, _ = self.check_launch(db)
            supervisor = self.ctx.supervisor
            from acc import providers

            from ..compiler import LaunchInputs, materialize, session_environment
            inputs = LaunchInputs.from_dict(session.launch_spec.inputs)
            if inputs.extra_args or inputs.initial_prompt or inputs.remote_control or inputs.endpoint:
                raise p.RelayError(p.ErrorCode.UNSUPPORTED)
            delivery = supervisor.vault.deliver(db, session.project_id, session.owner,
                session.host != 'local', supervisor.catalog, inputs.mcps, provider='codex', claude_auth=session.claude_auth)
            files = materialize(session.launch_spec.files, delivery.catalog, 'codex')
            env = session_environment(inputs, delivery.catalog, session.run_dir, files)
            delivery.check_env(env)
            guards = {name: p.sha256_digest(files[name].encode()) for name in ops.GUARD_FILES if name in files}
            policy_digest = p.sha256_digest(p.canonical_json(guards))
            from ..vault import Cipher
            installation = p.sha256_digest(Cipher(self.ctx.settings.data_dir / 'secrets/session_relay_cursor.key').key(create=True))
            self.binding = p.Binding(self.binding_id, installation, str(self.placement[2][0]), session.run_as, session.cwd,
                'codex', '', policy_digest)
            self.frozen_policy = {'ownershipMode': 'exclusive_writer', 'launchDigest': session.launch_spec.digest, 'guardDigest': policy_digest,
                                  'guardrails': json.loads(files['guardrails.json'])}
            args = providers.get('codex').relay_args(inputs, files=files)
            result = {'binding': dict(vars(self.binding)), 'runId': str(self.run_id),
                'spec': {'session_id': session.id, 'run_dir': session.run_dir, 'new_session': True, 'guard_digest': policy_digest,
                         'app_server_args': args[3:], 'ownership_mode': 'exclusive_writer'}, 'files': files, 'env': {**env, **delivery.env}}
            db.commit()
            return result

    @write_intent
    def launched(self, params):
        if params.keys() != {'nativeId', 'processIdentity', 'brokerIncarnation'} or not p.valid_opaque(params['nativeId']):
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        with self.ctx.db() as db:
            session, row = self.check_launch(db)
            if db.get(RelayRun, self.run_id) is not None:
                raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            self.binding = p.Binding(**{**vars(self.binding), 'native_conversation': params['nativeId']})
            self.fence = p.Fence(row.authority_epoch, params['brokerIncarnation'], self.manager.incarnation)
            session.native_id, session.observed_state = params['nativeId'], 'idle'
            db.get(SessionRun, self.run_id).verified_at = utcnow()
            self.coordinates = store.coordinates(session)
            db.add(RelayRun(run_id=self.run_id, session_id=session.id, binding_id=self.binding_id,
                identity={'coordinates': self.coordinates, 'binding': dict(vars(self.binding)),
                          'processIdentity': params['processIdentity'], 'hubIncarnation': self.manager.incarnation,
                          'brokerIncarnation': params['brokerIncarnation'], 'placement': self.placement},
                frozen_policy=self.frozen_policy, policy_revision=self.policy_revision))
            db.flush()  # Ownership FK requires the attested RelayRun first.
            writer_actor = None
            if self.provider == 'codex':
                who, writer_actor = self.resolve.in_transaction(db) if hasattr(self.resolve, 'in_transaction') else self.resolve()
                db.add(RelayOwnership(session_id=session.id, mode='exclusive_writer', view='chat', phase='owned',
                    source_run_id=self.run_id, authority={}))
                row.controller = p.to_wire(writer_actor)
                row.control_revision = 1
                self.writer_resolve, self.writer_actor = self.resolve, writer_actor
            store.append(db, row, 'run.changed', {'runId': str(self.run_id)})
            db.add(AuditLog(actor=session.owner, action='relay.enroll', target=session.id,
                           detail={'runId': self.run_id, 'bindingId': self.binding_id}))
            db.commit()
            return {'authorityEpoch': row.authority_epoch, 'hubIncarnation': self.manager.incarnation,
                    'writerActor': p.to_wire(writer_actor) if writer_actor else None}

    async def launch(self, identifier):
        try:
            with self.ctx.db() as db:
                session, _ = self.check_launch(db)
                account, host = session.run_as, session.host or 'local'
            client = self.ctx.broker.fresh(account, host)
            reader, writer = await client.session_runtime({'version': self.protocol_version, 'enrollment': self.token})
            self.peer = RuntimePeer(reader, writer, self.handle, lost=self.lost)
            result = await self.peer.call('launch', {})
            with self.ctx.db() as db:
                self.check_run(db)
            if result['runId'] != str(self.run_id) or result['bindingId'] != self.binding_id:
                raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            self.manager.runs[identifier] = self
        except BaseException:
            if self.peer:
                await self.peer.close()
            raise
        finally:
            self.manager.launches.pop(identifier, None)

    def lost(self):
        if self.manager.runs.get(self.session_id) is self:
            self.manager.runs.pop(self.session_id, None)
        if self.fence is not None:
            task = asyncio.create_task(write_in_thread(self.fence_lost))
            self.manager.background.add(task)
            task.add_done_callback(self.background_done)

    def background_done(self, task):
        self.manager.background.discard(task)
        if not task.cancelled():
            task.exception()

    @write_intent
    def fence_lost(self):
        self.terminal_grants.invalidate()
        with self.ctx.db() as db:
            row = store.lock(db, self.session_id)
            if row.current_run_id == self.run_id and row.binding_id == self.binding_id:
                was_sealed = row.sealed
                store.seal(db, row)
                row.capabilities = {}
                owner = db.get(RelayOwnership, self.session_id)
                if owner and owner.phase == 'owned' and owner.source_run_id == self.run_id:
                    # An already-sealed native exit still needs R1 recovery UI.
                    owner.phase = 'recovery_required'
                    store.append(db, row, 'switch.changed', {'state': owner.phase, 'target': owner.view}, priority=True)
                session = db.get(Session, self.session_id)
                session.last_error = 'Relay runtime disconnected; explicit recovery is required.'
                if not was_sealed:
                    session.observed_state = 'recovery_needed'
                    # EOF is not native exit evidence; preserve earlier final facts.
                    store.append(db, row, 'session.stateChanged', {'state': 'recovery_needed',
                        'exitOutcome': 'unknown', 'processExitConfirmed': False, 'finalEventsDrained': False}, priority=True)
                db.commit()

    def check_run(self, db, *, resolve=None, action='control'):
        leader = getattr(self.ctx.broker, 'authority', None)
        if leader:
            leader.check()
        row = store.lock(db, self.session_id)
        session = db.get(Session, self.session_id)
        native = db.get(SessionRun, self.run_id)
        if (row.sealed or not native or native.ended_at or row.current_run_id != self.run_id
                or row.binding_id != self.binding_id or row.authority_epoch != self.fence.authority_epoch
                or session.desired_state != 'running' or store.coordinates(session) != self.coordinates
                or self.manager.current_placement(db, session, provider=self.provider) != self.placement
                or security_settings.revision(db) != self.policy_revision):
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        if resolve:
            _, current = authority.current(self.ctx, db, resolve, self.session_id, action=action, expected_run=self.run_id,
                                           generation_checked=True)
            # current_placement checked the owner's budget in this transaction.
            if current.actor.principal != session.owner and budgets.paused(db, current.actor.principal, session.provider, project_id=session.project_id,
                              machine=session.host, account=session.run_as):
                raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
            return row, current
        return row, None

    @write_intent
    def issue(self, resolve, identifier, params):
        if len(self.grants) >= 4096:
            raise p.RelayError(p.ErrorCode.LIMIT)
        with self.ctx.db() as db:
            row, current = self.check_run(db, resolve=resolve)
            op = store.operation(db, self.session_id, identifier, current.actor)
            if current.delivery_scope is not None:
                from .delivery import check_operation
                check_operation(current.delivery_scope, op, params)
            receipt = store.receipt_from(op.receipt)
            digest = p.operation_digest(receipt.method, params)
            p.check_registered(receipt, actor=current.actor, session_id=self.session_id,
                run_id=str(self.run_id), method=receipt.method, payload_digest=digest)
            if op.authority != current.stamp():
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            if receipt.state is not p.ReceiptState.QUEUED or op.queue_seq is not None:
                return receipt
            # A submit-lock wait may outlive confirmation or the deadline.
            # Refuse before minting a token: broker callbacks are still final
            # fences, but a queued refusal must not become a transport error.
            if not op.confirmed or op.deadline <= store.utcnow():
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            owner = db.get(RelayOwnership, self.session_id)
            if owner and owner.mode == 'exclusive_writer' and receipt.method == 'turn.submit':
                bound_attachment = owner.authority.get('writerAttachment')
                attachment = getattr(resolve, 'attachment_id', None)
                service = current.delivery_scope is not None
                if not service and ((bound_attachment and attachment != bound_attachment) or (attachment and not bound_attachment)):
                    raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
                from .terminal_grants import NativeResolver
                native = isinstance(resolve, NativeResolver) and resolve.registry is self.terminal_grants
                if (owner.view != ('terminal' if native else 'chat') or (not service and row.controller != p.to_wire(current.actor))
                        or (type(params.get('expectedControlRevision')) is not int or params.get('expectedControlRevision') != row.control_revision)):
                    raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            operation = AdapterOperation(identifier, self.session_id, str(self.run_id), receipt.method, digest,
                current.actor, self.fence, int(op.deadline.timestamp() * 1000), params)
            token = secrets.token_hex(32)
            self.grants[token] = Grant(operation, resolve, current)
            db.commit()
            return token

    @write_intent
    def fail_queued(self, token):
        grant = self.grants.get(token)
        if grant is None:
            return
        with self.ctx.db() as db:
            row = store.lock(db, self.session_id)
            op = store.operation(db, self.session_id, grant.operation.operation_id, grant.operation.actor)
            if op.receipt['state'] == 'queued':
                store.transition(db, row, op, p.ReceiptState.FAILED, reason=p.FailureReason.AUTHORITY_ENDED)
                db.commit()

    @write_intent
    def resolve_grant(self, token):
        grant = self.grants.get(token)
        if grant is None or grant.operation.deadline_ms <= int(time.time() * 1000):
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        from ..admin_features import enabled, require
        if grant.operation.method == 'decision.reply':
            from .native_decisions import DecisionBinding
            if (not enabled(self.ctx.settings, 'session_relay')
                    and not isinstance(getattr(grant, 'native_decision', None), DecisionBinding)):
                raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        else:
            require(self.ctx.settings, 'session_relay')
        with self.ctx.db() as db:
            _, current = self.check_run(db, resolve=grant.resolve,
                action='switch' if grant.operation.method in ('control.take', 'writer.take') else 'control')
            authority.recheck(grant.authority, current)
            if grant.operation.method == 'decision.reply':
                from .native_decisions import check_grant
                effect = check_grant(self, db, grant)
                # Feature-off rollback permits only the authenticated, server-held
                # choice's current DENY effect; never trust a client choice label.
                if not enabled(self.ctx.settings, 'session_relay') and effect is not p.ChoiceEffect.DENY:
                    raise p.RelayError(p.ErrorCode.UNSUPPORTED)
            op = store.operation(db, self.session_id, grant.operation.operation_id, current.actor)
            if (op.deadline <= store.utcnow() or op.authority != current.stamp() or op.receipt['state'] not in ('queued', 'submitting', 'accepted', 'failed', 'uncertain') or not op.confirmed
                    or op.receipt['payloadDigest'] != grant.operation.payload_digest):
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            if grant.operation.method == 'turn.interrupt':
                from .interrupts import check_grant
                check_grant(self, db, grant)
            if grant.operation.method == 'writer.take':
                from .writers import public_ready
                row = store.get(db, self.session_id)
                if not public_ready(self.ctx, row):
                    raise p.RelayError(p.ErrorCode.UNSUPPORTED)
            value = operation_wire(grant.operation)
            from .stock_approval_frames import Resolver as StockResolver
            from .terminal_grants import NativeResolver
            value['origin'] = 'terminal' if isinstance(grant.resolve, (NativeResolver, StockResolver)) else 'chat'
            if current.delivery_scope is not None:
                value['deliveryWriter'] = current.delivery_scope['writer']
            if grant.operation.method == 'turn.interrupt':
                value['origin'] = grant.native_interrupt.view
            value['receiptState'] = op.receipt['state']
            if grant.staging is not None:
                if db.get(RelayRun, self.run_id).identity.get('payloads', {}).get(grant.operation.operation_id) != grant.staging:
                    raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
                value.pop('params')
                value['staging'] = grant.staging
            db.commit()
            return value

    @write_intent
    def staged(self, params):
        value = self.resolve_grant(params['grant'])
        operation = operation_from(value)
        expected = hashlib.sha256(operation.operation_id.encode()).hexdigest() + '.payload'
        digest = p.sha256_digest(p.canonical_json(p.to_wire(operation.params)))
        if params != {'grant': params['grant'], 'reference': expected, 'digest': digest}:
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        grant = self.grants[params['grant']]
        grant.staging = {'reference': expected, 'digest': digest}
        grant.operation = replace(grant.operation, params={})
        with self.ctx.db() as db:
            self.check_run(db, resolve=grant.resolve)
            run = db.get(RelayRun, self.run_id)
            refs = dict(run.identity.get('payloads', {}))
            refs[operation.operation_id] = grant.staging
            run.identity = {**run.identity, 'payloads': refs}
            db.commit()
        return True

    @write_intent
    def settle(self, token, result):
        grant = self.grants[token]
        with self.ctx.db() as db:
            row = store.lock(db, self.session_id)
            op = store.operation(db, self.session_id, grant.operation.operation_id, grant.operation.actor)
            if op.receipt['state'] in ('accepted', 'failed'):
                self.grants.pop(token, None)
                return store.receipt_from(op.receipt)
            evidence = result.get('evidence')
            if result.get('outcome') in ('accepted', 'rejected') and evidence:
                proof = p.Evidence(p.EvidenceKind(evidence['kind']), evidence['operationId'], evidence['runId'], evidence.get('nativeRef'))
                receipt = store.settle(db, row, op, evidence=proof, accepted=result['outcome'] == 'accepted')
            elif op.receipt['state'] == 'uncertain':
                # Disconnect/Stop can seal this in-flight receipt before the
                # dispatch await returns unknown. Preserve its revision/events;
                # conclusive late evidence still takes the branch above.
                receipt = store.receipt_from(op.receipt)
            else:
                receipt = store.transition(db, row, op, p.ReceiptState.UNCERTAIN)
            db.commit()
            self.grants.pop(token, None)
            return receipt

    @write_intent
    def publication(self, params):
        if params.get('kind')=='decision.closed':
            from .native_decisions import closed
            return closed(self,params)
        if params.get('kind')=='decision.applied':
            from .native_decisions import applied
            return applied(self,params)
        with self.ctx.db() as db:
            row, _ = self.check_run(db)
            if params.get('kind') in ('turn.completed', 'turn.interrupted', 'turn.failed'):
                store.complete_turn(db, row, params['operationId'], outcome=params['kind'].split('.')[1])
            elif params.get('kind') == 'message':
                session = db.get(Session, self.session_id)
                mask = self.ctx.supervisor.vault.redactor(db, session.vault_keys or []).text
                history.message(db, row, item_id=params['itemId'], role=params.get('role', 'assistant'), complete_text=params['text'],
                                source_epoch=self.run_id, source_revision=0, mask=mask)
            elif params == {'kind': 'unsupportedDecision'}:
                store.append(db, row, 'session.stateChanged', {'state': 'unsupported_decision'})
            elif params.get('kind') == 'decision':
                if not params['choices'] or set(params['choices']) - {'accept', 'decline', 'cancel'}:
                    raise p.RelayError(p.ErrorCode.UNSUPPORTED)
                labels = {'accept': 'Allow once', 'decline': 'Deny', 'cancel': 'Cancel turn'}
                choices = tuple(p.DecisionChoice(choice, labels[choice],
                    p.ChoiceEffect.ALLOW_ONCE if choice == 'accept' else p.ChoiceEffect.DENY) for choice in params['choices'])
                session = db.get(Session, self.session_id)
                mask = self.ctx.supervisor.vault.redactor(db, session.vault_keys or []).text
                description = params.get('description')
                if description is None or (isinstance(description, str) and not description.strip()):
                    # Older producers cannot authorize a blind Codex allow.
                    # Retain their explicit denial choices for safe recovery.
                    if self.provider in ('codex', 'claude'):
                        choices = tuple(choice for choice in choices if choice.effect is p.ChoiceEffect.DENY)
                        if not choices:
                            raise p.RelayError(p.ErrorCode.UNSUPPORTED)
                    description = ('Native command details are unavailable; only denial is available.'
                                   if self.provider == 'codex' else 'Review a native tool action.')
                if not isinstance(description, str) or len(description.encode()) > 7168:
                    raise p.RelayError(p.ErrorCode.LIMIT)
                approval = params.get('approval') if self.provider == 'claude' else None
                if self.provider == 'claude' and not matches_action(approval, params['actionDigest']):
                    choices = tuple(choice for choice in choices if choice.effect is p.ChoiceEffect.DENY)
                    if not choices:
                        raise p.RelayError(p.ErrorCode.UNSUPPORTED)
                    description = 'Complete tool details could not be verified; only denial is available.'
                    approval = {}  # Never present unbound facts as this request's command.
                store.add_decision(db, row, p.Decision(params['decisionId'], self.session_id, str(self.run_id),
                    p.DecisionKind.TOOL_APPROVAL, params['actionDigest'], mask(description), choices,
                    params['expiresAtMs'], self.policy_revision))
                if self.provider == 'claude':
                    from .approval_inbox import publish
                    publish(db, row, params['decisionId'], approval if isinstance(approval, dict) else {}, mask)
            elif params.get('kind') == 'exited':
                # Missing legacy status is unknown, never a successful exit.
                code = params.get('returnCode')
                if (params.keys() - {'kind', 'returnCode', 'finalScreen', 'finalEventsDrained'} or
                        (code is not None and (type(code) is not int or not -(1 << 31) <= code < (1 << 31)))):
                    raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
                drained = params.get('finalEventsDrained', False)
                if type(drained) is not bool:
                    raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
                from .final_screen import sanitize
                session = db.get(Session, self.session_id)
                mask = self.ctx.supervisor.vault.redactor(db, session.vault_keys or []).text
                screen = sanitize(params.get('finalScreen'), mask)
                outcome = 'unknown' if code is None else 'normal' if code == 0 else 'abnormal'
                store.seal(db, row)
                native = db.get(SessionRun, self.run_id)
                native.ended_at = utcnow()
                native.exit_status = code if code is not None and code >= 0 else None
                native.exit_signal = -code if code is not None and code < 0 else None
                native.end_reason = 'native_exit_' + outcome
                session = db.get(Session, self.session_id)
                session.observed_state = 'stopped' if outcome == 'normal' else 'recovery_needed'
                if outcome != 'normal':
                    session.last_error = 'Relay native owner ended without a clean exit; explicit recovery is required.'
                store.append(db, row, 'session.stateChanged', {'state': session.observed_state,
                    'exitOutcome': outcome, 'exitStatus': native.exit_status, 'exitSignal': native.exit_signal,
                    'finalScreen': screen, 'finalScreenAvailable': screen is not None, 'finalEventsDrained': drained}, priority=True)
            else:
                raise p.RelayError(p.ErrorCode.UNSUPPORTED)
            db.commit()
        return True

    async def handle(self, method, params):
        if method == 'authority.enroll':
            return await write_in_thread(lambda: self.enrollment(params))
        if method == 'authority.launch_check':
            def check():
                with self.ctx.db() as db:
                    self.check_launch(db)
                return True
            return await write_in_thread(check)
        if method == 'authority.launched':
            return await write_in_thread(lambda: self.launched(params))
        if method == 'authority.resolve':
            return await write_in_thread(lambda: self.resolve_grant(params['grant']))
        if method == 'native.approval_processed' and params.keys() == {'grant', 'sequence', 'operationId'}:
            return await write_in_thread(lambda: self.terminal_grants.approvals.processed(params['grant'], params['sequence'], params['operationId']))
        if method == 'native.approval_offer' and params.keys() == {'grant', 'offer'}:
            result = await write_in_thread(lambda: self.terminal_grants.approvals.arm(params['grant'], params['offer']))
            self.terminal_grants.approvals.watch()
            return result
        if method in ('native.approval_frame', 'native.approval_reply') and params.keys() == {'grant', 'sequence', 'alias', 'choice'}:
            approvals = self.terminal_grants.approvals
            args = (params['grant'], params['sequence'], params['alias'], params['choice'])
            if method == 'native.approval_frame':
                return await write_in_thread(lambda: approvals.authorize(*args))
            return await approvals.reply(*args)
        if method == 'native.submit' and params.keys() == {'grant', 'sequence', 'text', 'revision'}:
            return await self.terminal_grants.native_submit(params['grant'], params['sequence'], params['text'], params['revision'])
        if method == 'native.processed' and params.keys() == {'grant', 'sequence', 'operationId'}:
            return await write_in_thread(lambda: self.terminal_grants.native_processed(params['grant'], params['sequence'], params['operationId']))
        if method == 'authority.terminal' and params.keys() <= {'grant', 'action', 'sequence'}:
            return await write_in_thread(lambda: self.terminal_grants.authorize(
                params['grant'], params['action'], params.get('sequence')))
        if method == 'authority.receipt_read' and params.keys() == {'grant'}:
            from .receipt_read import service
            return await asyncio.to_thread(service(self).verify, params['grant'])
        if method == 'authority.terminal_read' and params.keys() == {'grant'}:
            from .terminal_read import service
            return await asyncio.to_thread(service(self).verify, params['grant'])
        if method == 'authority.native_read':
            def check_read():
                leader = getattr(self.ctx.broker, 'authority', None)
                if leader:
                    leader.check()
                with self.ctx.db() as db:
                    row, grant = authority.current(self.ctx, db, self.resolve, self.session_id, action='read', expected_run=self.run_id)
                    placement = self.manager.current_placement(db, db.get(Session, self.session_id), provider=self.provider)
                    return (row.binding_id == self.binding_id and str(grant.run_id) == str(self.run_id)
                            and placement == self.placement)
            return await write_in_thread(check_read)
        if method == 'authority.writer':
            @write_intent
            def check_writer():
                from ..admin_features import require
                require(self.ctx.settings, 'session_relay')
                token = params.get('grant')
                active = self.grants.get(token) if token is not None else None
                if token is not None:
                    if active is None or active.operation.method not in ('control.take', 'writer.take'):
                        raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
                    value = self.resolve_grant(token)
                    if value['receiptState'] != 'submitting':
                        raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
                resolve = active.resolve if active else self.writer_resolve
                actor = active.operation.actor if active else self.writer_actor
                with self.ctx.db() as db:
                    _, current = self.check_run(db, resolve=resolve, action='switch' if active else 'control')
                    return params.get('actor') == p.to_wire(actor) and current.actor == actor
            return await write_in_thread(check_writer)
        if method == 'authority.verify':
            value = await write_in_thread(lambda: self.resolve_grant(params['grant']))
            incoming = dict(params['operation'])
            payload = incoming.pop('params')
            value.pop('staging', None)
            value.pop('deliveryWriter', None)  # authenticated route binding, not operation identity
            state = value.pop('receiptState')
            return (value == incoming and state == 'submitting'
                    and p.operation_digest(incoming['method'], payload) == incoming['payloadDigest'])
        if method == 'authority.staged':
            return await write_in_thread(lambda: self.staged(params))
        if method == 'authority.discard':
            grant = self.grants[params['grant']]
            with self.ctx.db() as db:
                op = store.operation(db, self.session_id, grant.operation.operation_id, grant.operation.actor)
                if op.receipt['state'] != 'failed':
                    raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
                return {'operationId': op.id, 'reference': hashlib.sha256(op.id.encode()).hexdigest() + '.payload'}
        if method == 'publication':
            result = await write_in_thread(lambda: self.publication(params))
            if params.get('kind') == 'decision.closed':
                await self.terminal_grants.approvals.resolved(params['decisionId'], None)
            if params.get('kind') == 'decision.applied':
                await self.terminal_grants.approvals.application_published(params)
            if params.get('kind') in ('turn.completed', 'turn.interrupted', 'turn.failed') and (self.drain_task is None or self.drain_task.done()):
                self.drain_task = asyncio.create_task(self.manager.drain(self))
            return result
        if method == 'authority.stop':
            with self.ctx.db() as db:
                row = store.get(db, self.session_id)
                return row.sealed and row.current_run_id == self.run_id and row.binding_id == self.binding_id
        raise p.RelayError(p.ErrorCode.UNSUPPORTED)


_MANAGER_LOCK = threading.Lock()


def manager(ctx):
    runtime = getattr(ctx, 'relay_runtime', None)
    if runtime is None:
        with _MANAGER_LOCK:
            runtime = getattr(ctx, 'relay_runtime', None)
            if runtime is None:
                runtime = RuntimeManager(ctx)
                ctx.relay_runtime = runtime
                ctx.supervisor.relay_manager = runtime
    return runtime
