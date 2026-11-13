"""Durable idle ownership plans. Read attachments never create or claim a plan.

This is independent of native RPC correlation: a single writer is fenced before
idle/drain/stop, and a replacement writer is admitted only after verified exit.
The public capability/negotiation gates remain unchanged (and unqualified).
"""
from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select

from acc.session_relay import protocol as p

from .. import budgets, claude_subscription
from ..admin_features import Features, enabled
from ..models import RelayDecision, RelayOperation, RelayOwnership, RelayRun, Session, SessionRun, User, utcnow
from . import store
from .hub import Hub

MODE = 'exclusive_idle'
PUBLIC_PLAN_FIELDS = frozenset({'_sourceAuthority','_requestAttachment'})


class OwnershipHub(Hub):
    def supplied_operation(self,db,row,grant,identifier,method,params):
        from .exclusive_public import public_ready
        if not public_ready(self.ctx,row) or grant.actor.kind is not p.ActorKind.PERSON:
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        operation=store.operation(db,row.session_id,identifier,grant.actor)
        p.check_registered(store.receipt_from(operation.receipt),actor=grant.actor,session_id=row.session_id,
            run_id=str(grant.run_id),method=method,payload_digest=p.operation_digest(method,params))
        if (operation.authority!=grant.stamp() or operation.deadline<=utcnow()
                or operation.receipt['state']!='queued' or operation.queue_seq is not None):
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        return identifier

    def __init__(self, ctx, *, placement=None):
        super().__init__(ctx)
        self.placement = placement or self._placement

    def _placement(self, db, session):
        from .runtime import manager
        return manager(self.ctx).current_placement(db, session, provider=session.provider)

    def _policy(self, db, session, grant, run, *, allow_disabled=False):
        if (not allow_disabled and not enabled(self.ctx.settings, 'session_relay')) or session.provider != 'claude':
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        placement = self.placement(db, session)
        if run.identity.get('placement') != placement:
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        if budgets.paused(db, grant.actor.principal, session.provider, project_id=session.project_id,
                          machine=session.host or 'local', account=session.run_as):
            raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
        if session.provider == 'claude':
            mode = run.frozen_policy.get('authMode')
            if mode == 'subscription':
                user = db.scalar(select(User).where(User.username == grant.actor.principal))
                if (not user or user.id != run.frozen_policy.get('personId') or
                        not claude_subscription.allowed(db, user.id, Features(self.ctx.settings))):
                    raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
            elif mode != 'api_key':
                raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
        return placement

    @staticmethod
    def _pending(db, row, *, ignore=None):
        # A refusal is audited in a new transaction. Exhaust these cursors so
        # its exception traceback cannot retain a stale SQLite read snapshot.
        operations = db.scalars(select(RelayOperation).where(RelayOperation.session_id == row.session_id)).all()
        if row.active_operation_id or any(op.receipt['state'] in ('submitting', 'uncertain') or
                (op.queue_seq is not None and op.receipt['state'] == 'queued') for op in operations if op.id != ignore):
            raise p.RelayError(p.ErrorCode.BUSY)
        if any(record.decision['status'] in ('pending', 'reserved', 'outcome_unknown') for record in
               db.scalars(select(RelayDecision).where(RelayDecision.session_id == row.session_id)).all()):
            raise p.RelayError(p.ErrorCode.BUSY)

    def bind(self, resolve, session_id, *, view):
        """Internal reviewed enrollment only; never called from attach/preference.

        No native process is adopted and no capability is promoted here. The
        broker's launch callback must have recorded the verified owner first.
        """
        def work(db, row, grant):
            run = db.get(RelayRun, grant.run_id)
            self._policy(db, db.get(Session, session_id), grant, run)
            if (view not in ('chat', 'terminal') or run.frozen_policy.get('ownershipMode') != MODE
                    or not run.identity.get('processIdentity') or db.get(RelayOwnership, session_id)):
                raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            db.add(RelayOwnership(session_id=session_id, mode=MODE, view=view, phase='owned',
                                  source_run_id=grant.run_id, authority=grant.stamp()))
            return {'view': view, 'runId': str(grant.run_id), 'revision': row.revision}
        return self._write(resolve, session_id, work, action='switch')

    @staticmethod
    def snapshot(row, owner):
        return {'runId': str(row.current_run_id), 'planId': owner.plan_id, 'planDigest': owner.digest, 'view': owner.view,
                'target': owner.target, 'phase': owner.phase, 'revision': row.revision,
                'sourceRunId': str(owner.source_run_id),
                'targetRunId': str(owner.target_run_id) if owner.target_run_id else None,
                'prepareOperationId': owner.prepare_operation_id, 'operationId': owner.commit_operation_id}

    def register_switch(self, resolve, session_id, expected_run, key, method, digest):
        """Reviewed L1 entry; the dormant public extension does not widen R1 methods."""
        def work(db, row, grant):
            owner = db.get(RelayOwnership, session_id)
            self._policy(db, db.get(Session, session_id), grant, db.get(RelayRun, grant.run_id),
                allow_disabled=method == 'switch.cancel')
            if not owner or owner.mode != MODE or method not in ('switch.prepare', 'switch.commit', 'switch.cancel'):
                raise p.RelayError(p.ErrorCode.UNSUPPORTED)
            return store.register(db, row, grant.actor, key, method, digest, grant.stamp(),
                methods={'switch.prepare', 'switch.commit', 'switch.cancel'})
        return self._write(resolve, session_id, work, action='switch', expected_run=expected_run)

    @staticmethod
    def _registered(db, row, grant, identifier, method, params):
        op = store.operation(db, row.session_id, identifier, grant.actor)
        receipt = store.receipt_from(op.receipt)
        p.check_registered(receipt, actor=grant.actor, session_id=row.session_id,
            run_id=str(grant.run_id), method=method, payload_digest=p.operation_digest(method, params))
        if op.authority != grant.stamp() or op.deadline <= utcnow():
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        return op

    def prepare(self, resolve, session_id, *, target, revision, request_key, operation_id=None):
        def work(db, row, grant):
            owner = db.get(RelayOwnership, session_id)
            run = db.get(RelayRun, grant.run_id)
            self._policy(db, db.get(Session, session_id), grant, run)
            if not owner or owner.mode != MODE or target not in ('chat', 'terminal') or not p.valid_opaque(request_key):
                raise p.RelayError(p.ErrorCode.UNSUPPORTED)
            params = {'sessionId': session_id, 'expectedRunId': str(grant.run_id),
                      'target': target, 'expectedRevision': revision}
            registered = None
            if operation_id is not None:
                if request_key != operation_id:
                    raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
                registered = self._registered(db, row, grant, operation_id, 'switch.prepare', params)
            # Duplicate activation observes one outcome. It cannot launch again.
            if owner.request_key == request_key:
                if registered is not None and (owner.prepare_operation_id != operation_id
                        or registered.receipt['state'] != 'accepted'):
                    raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
                if owner.authority['actor'] != p.to_wire(grant.actor) or owner.target != target:
                    raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
                return self.snapshot(row, owner)
            if revision != row.revision:
                raise p.RelayError(p.ErrorCode.REVISION_CONFLICT)
            if owner.phase == 'prepared' and owner.expires_at <= utcnow():
                owner.phase = 'owned'  # no stop or dispatch was claimed
            if owner.phase != 'owned':
                raise p.RelayError(p.ErrorCode.BUSY)
            if target == owner.view:
                if registered is not None:
                    if registered.receipt['state'] == 'queued':
                        store.transition(db, row, registered, p.ReceiptState.SUBMITTING)
                        store.settle(db, row, registered, accepted=True,
                            evidence=p.Evidence(p.EvidenceKind.HUB_COMMIT, registered.id, str(registered.run_id)))
                    elif registered.receipt['state'] != 'accepted':
                        raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
                return self.snapshot(row, owner)
            if registered is not None and registered.receipt['state'] != 'queued':
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            self._pending(db, row, ignore=registered.id if registered else None)
            if registered is None:
                registration = store.register(db, row, grant.actor, 'owner-prepare-' + p.sha256_digest(request_key.encode()).split(':')[-1],
                    'switch.prepare', p.operation_digest('switch.prepare', params), grant.stamp(), methods={'switch.prepare'})
                operation_id_value = registration.operation_id
            else:
                operation_id_value = registered.id
            source = {key: value for key, value in owner.authority.items() if key not in PUBLIC_PLAN_FIELDS}
            owner.prepare_operation_id, owner.commit_operation_id = operation_id_value, None
            owner.plan_id, owner.request_key, owner.target = uuid.uuid4().hex, request_key, target
            owner.source_run_id, owner.target_run_id = grant.run_id, None
            owner.previous_actor = owner.authority.get('actor')
            owner.authority, owner.expires_at = grant.stamp(), utcnow() + timedelta(seconds=60)
            if operation_id is not None and getattr(resolve, 'attachment_id', None) is not None:
                owner.authority={**owner.authority,'_sourceAuthority':source,
                    '_requestAttachment':resolve.attachment_id}
            owner.digest = p.sha256_digest(p.canonical_json({'planId': owner.plan_id, 'target': target,
                'authority': owner.authority, 'revision': revision, 'mode': MODE}))
            owner.phase = 'prepared'
            self._audit(db, grant.actor, session_id, 'owner.prepare', previous=owner.previous_actor,
                        plan_id=owner.plan_id, target=target, source_run_id=grant.run_id)
            row.revision += 1
            store.append(db, row, 'switch.changed', {'state': owner.phase, 'target': target})
            op = db.get(RelayOperation, owner.prepare_operation_id)
            store.transition(db, row, op, p.ReceiptState.SUBMITTING)
            store.settle(db, row, op, accepted=True, evidence=p.Evidence(p.EvidenceKind.HUB_COMMIT, op.id, str(op.run_id)))
            return self.snapshot(row, owner)
        return self._write(resolve, session_id, work, action='switch')

    def _plan(self, db, row, grant, plan_id, digest):
        owner = db.get(RelayOwnership, row.session_id)
        if (not owner or owner.plan_id != plan_id or owner.digest != digest or owner.mode != MODE
                or {key:value for key,value in owner.authority.items() if key not in PUBLIC_PLAN_FIELDS} != grant.stamp() or owner.source_run_id != grant.run_id
                or owner.expires_at is None or owner.expires_at <= utcnow()):
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        self._policy(db, db.get(Session, row.session_id), grant, db.get(RelayRun, grant.run_id))
        self._pending(db, row, ignore=owner.commit_operation_id)
        return owner

    def claim(self, resolve, session_id, plan_id, digest, *, revision, operation_id=None):
        def work(db, row, grant):
            owner = self._plan(db, row, grant, plan_id, digest)
            if owner.phase != 'prepared' or row.revision != revision:
                raise p.RelayError(p.ErrorCode.REVISION_CONFLICT)
            params = {'sessionId': session_id, 'expectedRunId': str(grant.run_id), 'planId': plan_id,
                      'planDigest': digest, 'expectedRevision': revision}
            if operation_id is None:
                registration = store.register(db, row, grant.actor, 'owner-commit-' + owner.plan_id,
                    'switch.commit', p.operation_digest('switch.commit', params), grant.stamp(), methods={'switch.commit'})
                registered = db.get(RelayOperation, registration.operation_id)
            else:
                if owner.authority.get('_requestAttachment') != getattr(resolve, 'attachment_id', None):
                    raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
                registered = self._registered(db, row, grant, operation_id, 'switch.commit', params)
                if registered.receipt['state'] != 'queued':
                    raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            owner.commit_operation_id = registered.id
            store.transition(db, row, registered, p.ReceiptState.SUBMITTING)
            native = SessionRun(session_id=session_id, reason='relay_owner_switch')
            db.add(native)
            db.flush()
            owner.target_run_id, owner.phase = native.id, 'draining'
            row.revision += 1
            store.append(db, row, 'switch.changed', {'state': 'draining', 'target': owner.target})
            return self.snapshot(row, owner)
        return self._write(resolve, session_id, work, action='switch')

    def verify(self, resolve, session_id, plan_id, digest):
        def work(db, row, grant):
            owner = self._plan(db, row, grant, plan_id, digest)
            if owner.phase not in ('draining', 'stopping', 'starting'):
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            return self.snapshot(row, owner)
        return self._write(resolve, session_id, work, action='switch')

    def checkpoint(self, resolve, session_id, plan_id, digest, phase, evidence):
        """Broker-authenticated proof only, not an HTTP-provided boolean."""
        def work(db, row, grant):
            owner = self._plan(db, row, grant, plan_id, digest)
            run = db.get(RelayRun, grant.run_id)
            binding = run.identity['binding']
            if phase in ('stopping', 'starting'):
                if (evidence.get('idle') is not True or evidence.get('native_conversation') != binding['native_conversation']
                        or evidence.get('policy_digest') != binding['launch_policy_digest']):
                    raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
                expected = 'draining' if phase == 'stopping' else 'stopping'
                if owner.phase != expected or evidence.get('exit') != ('running' if phase == 'stopping' else 'exited'):
                    raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
                retained = evidence.get('retained_screen')
                if retained is not None:
                    if phase != 'starting' or owner.view != 'terminal':
                        raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
                    from .final_screen import sanitize
                    session = db.get(Session, session_id)
                    mask = self.ctx.supervisor.vault.redactor(db, session.vault_keys or []).text
                    screen = sanitize(retained, mask)
                    if screen is not None:
                        store.append(db, row, 'session.stateChanged', {'state': 'retired',
                            'exitOutcome': 'unknown', 'processExitConfirmed': True,
                            'finalEventsDrained': False, 'finalScreen': screen}, priority=True)
                owner.phase = phase
            elif phase == 'owned':
                if (owner.phase != 'starting' or evidence.get('binding_id') != row.binding_id
                        or evidence.get('run_id') != str(owner.target_run_id)
                        or evidence.get('native_conversation') != binding['native_conversation']
                        or not evidence.get('process_identity')):
                    raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
                previous = db.get(SessionRun, grant.run_id)
                replacement = db.get(SessionRun, owner.target_run_id)
                if previous.ended_at or replacement.verified_at or replacement.ended_at:
                    raise p.RelayError(p.ErrorCode.STALE_RUN)
                previous.ended_at, replacement.verified_at = utcnow(), utcnow()
                db.add(RelayRun(run_id=replacement.id, session_id=session_id, binding_id=row.binding_id,
                    identity={**run.identity, 'processIdentity': evidence['process_identity'], 'writerView': owner.target},
                    frozen_policy=run.frozen_policy, policy_revision=run.policy_revision))
                # A reviewed replacement revokes every old-run grant. Retained
                # reads survive, and capabilities remain deliberately empty.
                store.seal(db, row)
                row.current_run_id, row.sealed, row.capabilities = replacement.id, False, {}
                op = db.get(RelayOperation, owner.commit_operation_id)
                store.settle(db, row, op, accepted=True, evidence=p.Evidence(p.EvidenceKind.HUB_COMMIT, op.id, str(op.run_id)))
                owner.view, owner.phase = owner.target, 'owned'
                owner.source_run_id = replacement.id
            else:
                raise p.RelayError(p.ErrorCode.UNSUPPORTED)
            row.revision += 1
            store.append(db, row, 'switch.changed', {'state': owner.phase, 'view': owner.view})
            return self.snapshot(row, owner)
        return self._write(resolve, session_id, work, action='switch')

    def abort(self, resolve, session_id, plan_id, *, operation_id=None, previous_authority=None):
        def work(db, row, grant):
            owner = db.get(RelayOwnership, session_id)
            if not owner or owner.plan_id != plan_id or owner.authority['actor'] != p.to_wire(grant.actor):
                raise p.RelayError(p.ErrorCode.NOT_FOUND_OR_DENIED)
            registered = None
            if operation_id is not None:
                params = {'sessionId': session_id, 'expectedRunId': str(grant.run_id), 'planId': plan_id}
                registered = self._registered(db, row, grant, operation_id, 'switch.cancel', params)
                if owner.phase != 'prepared' or registered.receipt['state'] != 'queued':
                    raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
                if (previous_authority is None or any(previous_authority.get(key) != value
                        for key, value in grant.stamp().items() if key != 'actor')):
                    raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
                owner.authority = dict(previous_authority)
            if owner.phase in ('stopping', 'starting', 'recovery_required'):
                owner.phase = 'recovery_required'
                store.seal(db, row)
            elif owner.phase in ('prepared', 'draining'):
                owner.phase = 'owned'
            else:
                return self.snapshot(row, owner)
            if owner.target_run_id and owner.phase == 'owned':
                db.get(SessionRun, owner.target_run_id).ended_at = utcnow()
                if owner.commit_operation_id:
                    op = db.get(RelayOperation, owner.commit_operation_id)
                    store.settle(db, row, op, accepted=False, evidence=p.Evidence(
                        p.EvidenceKind.BROKER_REFUSAL, op.id, str(op.run_id)))
            if registered is not None:
                store.transition(db, row, registered, p.ReceiptState.SUBMITTING)
                store.settle(db, row, registered, accepted=True,
                    evidence=p.Evidence(p.EvidenceKind.HUB_COMMIT, registered.id, str(registered.run_id)))
            row.revision += 1
            store.append(db, row, 'switch.changed', {'state': owner.phase, 'view': owner.view})
            return self.snapshot(row, owner)
        return self._write(resolve, session_id, work, action='switch')
