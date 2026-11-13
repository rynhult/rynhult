"""Exclusive-writer broker controller over the authenticated runtime channel.

Native permission hooks terminate in the hub; no second provider client is
started to observe a TUI. Production input capabilities remain unqualified.
"""
from __future__ import annotations

import asyncio
import hashlib
from dataclasses import asdict, replace

from acc.session_relay import protocol as p
from acc.session_relay.adapters.base import AdmissionResult, CloseMode, ExitState, Outcome, ReviewedSpec
from acc.session_relay.adapters.claude_switch import IdleOwnerSwitch

from .relay_runtime import BrokerRuntime
from .session_relay import AdmissionBroker, AdmissionJournal, PayloadStaging

APPLICATION_OBSERVATION_SECONDS = 30


class OwnershipController:
    def __init__(self, *, spec, view, owner, factory, peer):
        self.spec, self.factory, self.peer = spec, factory, peer
        self.plan = None
        self.fenced = False
        self.closed = False
        self.switch = IdleOwnerSwitch(spec=spec, view=view, owner=owner,
            launch_chat=self.chat, launch_terminal=self.terminal,
            authorize=self.authorize_switch, checkpoint=self.checkpoint,
            preflight=getattr(factory, 'preflight', None))

    async def authorize_switch(self, source, target):
        if self.closed or not self.plan or self.peer.closed or not getattr(self.factory, 'exit_qualified', False):
            return False
        return await self.peer.call('authority.switch_verify', {'plan': self.plan}) is True

    async def checkpoint(self, phase, proof):
        evidence = p.to_wire(asdict(proof))
        # stop_owner has proved idle and whole-tree exit, while the old pane
        # still exists. Preserve only its captured view, never a replacement's
        # screen or an input grant. prepare() retires this pane next.
        owner = getattr(self.factory, 'current', None)
        source = getattr(owner, 'spec', None)
        if (phase == 'starting' and self.switch.view == 'terminal' and proof.idle
                and proof.exit is ExitState.EXITED and source is not None
                and source.run_id == self.spec.run_id and source.binding == self.spec.binding):
            from .claude_owner import TmuxProcess
            process = getattr(owner, 'process', None)
            if isinstance(process, TmuxProcess) and process.returncode is not None:
                text = process.snapshot
                # TmuxProcess keeps at most 128Ki characters. Refuse that
                # boundary rather than retaining a possibly clipped secret.
                if text and len(text.encode()) < 128 << 10:
                    screen = {'text': text, 'truncated': True}  # bounded pane history
                    if len(p.canonical_json(screen)) <= 120 << 10:
                        evidence['retained_screen'] = screen
        await self.peer.call('authority.switch_checkpoint', {
            'plan': self.plan, 'phase': phase, 'evidence': evidence})

    async def chat(self, target):
        return await self.factory.chat(target)

    async def terminal(self, target):
        return await self.factory.terminal(target)

    async def select(self, token):
        if self.closed or self.fenced or self.plan is not None:
            raise p.RelayError(p.ErrorCode.BUSY)
        # Only an opaque plan handle is accepted. No actor/spec from the client.
        plan = await self.peer.call('authority.switch_resolve', {'plan': token})
        if plan['sourceRunId'] != self.spec.run_id or plan['bindingId'] != self.spec.binding.binding_id:
            raise p.RelayError(p.ErrorCode.STALE_RUN)
        self.plan, self.fenced = token, True
        target = replace(self.spec, run_id=plan['targetRunId'])
        try:
            result = await self.switch.select(plan['target'], target)
            self.spec = self.switch.spec
            return {'view': result.view, 'runId': result.run_id, 'changed': result.changed}
        except BaseException:
            # A busy pre-stop refusal may release the fence; a stopped/unknown
            # owner never becomes resumable through a retry of this activation.
            try:
                await self.peer.call('authority.switch_failed', {'plan': token,
                    'recoveryRequired': self.switch.recovery_required})
            except (ConnectionError, TimeoutError):
                pass
            if self.switch.recovery_required:
                self.closed = True
                if self.switch.owner:
                    await self.switch.owner.close(CloseMode.STOP)
            raise
        finally:
            self.plan = None
            self.fenced = self.switch.recovery_required

    def writer(self, view):
        if self.closed or self.fenced or self.switch.recovery_required or view != self.switch.view:
            raise p.RelayError(p.ErrorCode.BUSY)
        return self.switch.owner

    async def close(self):
        self.closed = self.fenced = True
        # The factory owns the current process and its conclusive cleanup.
        # Closing the adapter twice can interrupt SDK permission-task teardown.
        await self.factory.close()


class ExclusiveRuntime(BrokerRuntime):
    """Version 3 runtime. Constructed only for an authorized broker connection."""
    def __init__(self, factory, enrollment):
        super().__init__(factory, enrollment)
        self.controller = self.owners = self.hooks = None
        self.answer_grant = None
        self.active_operation = None
        self.service_operations = {}
        self.native_transcript = None
        self.application_tasks = set()
        self.recovery_plan = None
        self.application_records = {}
        self.application_inflight = set()
        self.application_proofs = None

    def lost(self):
        super().lost()
        if self.controller:
            self.controller.closed = self.controller.fenced = True

    async def owner_authorized(self):
        return (self.peer is not None and not self.peer.closed and
                await self.peer.call('authority.owner', {'runId': self.spec.run_id, 'operationId': self.active_operation}) is True)

    async def publish(self, value):
        return await self.peer.call('publication', {**value, 'runId': self.spec.run_id})

    async def observe(self, payload):
        if payload.get('hook_event_name') == 'SessionStart' and isinstance(payload.get('transcript_path'), str):
            self.native_transcript = (self.spec.run_id, payload['transcript_path'])
        owner = self.owners.current if self.owners is not None else None
        if owner is not None and hasattr(owner, 'observe_hook'):
            owner.observe_hook(payload)
            callback = getattr(owner.host, 'observe_native', None) if hasattr(owner, 'host') else None
            if callback:
                await callback(payload)
        elif self.owners is not None:
            # Startup observations cannot establish idle. Preserve a bounded
            # ledger until the owned TUI object exists; reject work meanwhile.
            if payload.get('hook_event_name') not in ('SessionStart', 'Notification', 'SessionEnd'):
                raise p.RelayError(p.ErrorCode.BUSY)

    async def launch(self):
        from .claude_owner import ClaudeOwnerFactory
        value = await self.peer.call('authority.enroll', {'enrollment': self.enrollment, 'linkId': 'exclusive'})
        spec = ReviewedSpec(p.Binding(**value['binding']), value['runId'], value['spec'])
        if spec.binding.provider != 'claude' or spec.spec.get('ownership_mode') != 'exclusive_idle':
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        from .claude_paths import check
        check(self.config,spec)
        from pathlib import Path
        # Resolve only the authenticated account broker's config. Main's machine
        # and client fields cannot choose a CLI path on this account. The owner
        # factory then checks the pinned bytes and file incarnation before use.
        try:
            executable = Path(self.config.providers['claude']).resolve(strict=True)
        except (KeyError, OSError):
            raise p.RelayError(p.ErrorCode.UNSUPPORTED) from None
        spec = replace(spec, spec={**spec.spec, 'cli_path': str(executable)})
        self.spec, self.binding = spec, spec.binding
        self.key = spec.spec['session_id'], spec.run_id
        async with self.factory.lock:
            if self.key in self.factory.active or len(self.factory.active) >= 2048:
                raise p.RelayError(p.ErrorCode.LIMIT)
            self.factory.active[self.key] = self
        self.owners = ClaudeOwnerFactory(self.factory, spec, value, self)
        self.run_dir = __import__('pathlib').Path(spec.spec['run_dir'])
        self.guards = self.owners.guards
        self.journal = AdmissionJournal(self.owners.directory / 'journal')
        self.staging = PayloadStaging(self.owners.directory / 'payloads')
        from .application_proofs import ApplicationProofs
        self.application_proofs = ApplicationProofs(self.owners.directory)
        self.broker = AdmissionBroker(self.journal, self.verify)
        first = await self.owners.chat(spec, initial=True)
        response = await self.peer.call('authority.launched', {'nativeId': spec.binding.native_conversation,
            'processIdentity': first.attestation.process_identity, 'brokerIncarnation': self.broker.incarnation})
        self.adapter = first.owner
        await self.broker.enroll(p.Fence(response['authorityEpoch'], self.broker.incarnation, response['hubIncarnation']), self.link_id)
        self.ready = True
        self.controller = OwnershipController(spec=spec, view='chat', owner=first.owner,
                                             factory=self.owners, peer=self.peer)
        return {'runId': spec.run_id, 'bindingId': spec.binding.binding_id, 'capabilities': {}}

    async def handle(self, method, params):
        if method == 'launch' and params == {} and self.spec is None:
            return await self.launch()
        if not self.controller or self.peer.closed:
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        if method == 'owner.select' and params.keys() == {'plan'}:
            async with self.operation_lock, self.broker.lock:
                result = await self.controller.select(params['plan'])
                self.spec = self.controller.spec
                self.adapter = self.controller.switch.owner
                self.terminal_host = self.adapter.host if self.controller.switch.view == 'terminal' else None
            fence = await self.peer.call('authority.owner_fence', {})
            await self.broker.enroll(p.Fence(fence['authorityEpoch'], self.broker.incarnation, fence['hubIncarnation']), self.link_id)
            return result
        if method == 'owner.inspect' and params.keys() == {'observation'}:
            self.reconcile_late_applications()
            from .claude_observation import inspect_owner
            return await inspect_owner(self, params['observation'])
        if method == 'owner.recover' and params.keys()=={'plan'}:
            async with self.operation_lock,self.broker.lock:
                from .relay_recovery import recover
                result=await recover(self,params['plan'])
            fence=await self.peer.call('authority.owner_fence',{})
            await self.broker.enroll(p.Fence(fence['authorityEpoch'],self.broker.incarnation,fence['hubIncarnation']),self.link_id)
            return result
        if method == 'owner.answer' and params.keys() == {'grant'}:
            return await self.answer(params)
        if method == 'stop' and params == {}:
            async with self.operation_lock:
                if await self.peer.call('authority.stop', {}) is not True:
                    raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
                self.stopping = True
                await self.controller.close()
                return {'exited': await self.owners.exited()}
        return await super().handle(method, params)

    async def terminal_value(self, token, action, sequence=None):
        from .claude_terminal_runtime import terminal_value
        if action == 'input' and getattr(self, 'native_prompt_active', False):
            raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
        value = await terminal_value(self, token, action, sequence)
        if action == 'input' and getattr(self, 'native_prompt_active', False):
            raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
        return value

    async def answer(self, params):
        async with self.operation_lock:
            value = await self.peer.call('authority.answer', params)
            if getattr(self, 'native_prompt_active', False) and value['effect'] == 'allow_once':
                raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
            async def verify():
                return await self.peer.call('authority.answer_verify', params) is True
            self.answer_grant = params['grant']
            try:
                if self.owners.hooks and value['decisionId'] in self.owners.hooks.pending:
                    from . import claude_allow_evidence, claude_denial_evidence
                    if claude_allow_evidence.CLAUDE_TUI_ALLOW_ACK_QUALIFIED and value['effect'] == 'allow_once':
                        return await claude_allow_evidence.answer(self, value, verify)
                    if claude_denial_evidence.CLAUDE_TUI_DENY_ACK_QUALIFIED and value['effect'] == 'deny':
                        return await claude_denial_evidence.answer(self, value, verify)
                    result = await self.owners.hooks.answer(value['decisionId'], value['actionDigest'], value['effect'], verify)
                    # Only an exact completed native denial may retire its tool
                    # ledger entry. Socket closure and Stop never clear tools.
                    application = result.get('application', {})
                    if result.get('outcome') == 'accepted' and application.get('effect') == 'deny' and application.get('nativeToolId'):
                        self.owners.current.confirm_denial(application['nativeToolId'])
                    await self.persist_application(value, result)
                    self.observe_later(value, self.owners.current, self.owners.hooks)
                    return result
                from .relay_runtime import operation_from
                adapter = self.controller.writer('chat')
                if not await verify():
                    raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
                operation = operation_from(value['operation'])
                await adapter.reply_verified(operation, value['effect'])
                result = await adapter.observe_application(operation, value['effect'])
                if result.get('outcome') == 'accepted' and value['effect'] == 'deny':
                    adapter.confirm_denial(result['application']['nativeToolId'])
                await self.persist_application(value, result)
                self.observe_later(value, adapter, adapter)
                return result
            finally:
                self.answer_grant = None

    async def persist_application(self, value, result):
        if result.get('outcome') != 'accepted':
            return
        try:
            self.guard_check()
            await asyncio.to_thread(self.application_proofs.remember, self.binding, value, result['application'])
            self.guard_check()
        except (p.RelayError, OSError, ValueError, asyncio.CancelledError):
            # Cancellation during fsync must not bypass the post-await guard.
            # forget serializes behind the writer even if its thread outlives
            # the cancelled await; no unqualified fact survives that boundary.
            self.application_records.pop(value['operation']['operationId'], None)
            await asyncio.to_thread(self.application_proofs.forget, value['operation']['operationId'])
            raise

    def observe_later(self, value, owner, source):
        self.prune_applications()
        identifier = value['operation']['operationId']
        if identifier not in self.application_records and len(self.application_records) >= 128:
            return  # unknown remains explicit; no global capacity or provider retry
        self.application_records[identifier] = (value, owner, source)
        self.start_application_observer(identifier)

    def prune_applications(self):
        for identifier, (value, owner, _) in list(self.application_records.items()):
            if self.peer.closed or self.owners.current is not owner or self.spec.run_id != value['operation']['runId']:
                self.application_records.pop(identifier, None)

    def reconcile_late_applications(self):
        # Existing owner reads/provider completion may inspect retained proof,
        # never answer, claim input, send a query or restart a provider.
        self.prune_applications()
        for identifier in list(self.application_records)[:4]:
            record = self.application_records.pop(identifier)
            self.application_records[identifier] = record  # bounded fair rotation
            self.start_application_observer(identifier, once=True)

    def start_application_observer(self, identifier, *, once=False):
        if identifier in self.application_inflight or len(self.application_tasks) >= 16:
            return
        record = self.application_records.get(identifier)
        if record is None:
            return
        self.application_inflight.add(identifier)
        async def observe():
            try:
                await self.observe_application(*record, once=once)
            finally:
                self.application_inflight.discard(identifier)
        task = asyncio.create_task(observe())
        self.application_tasks.add(task)
        def finished(done):
            self.application_tasks.discard(done)
            if not done.cancelled():
                done.exception()
        task.add_done_callback(finished)

    async def observe_application(self, value, owner, source, *, once=False):
        from .relay_runtime import operation_from
        operation = operation_from(value['operation'])
        def trusted():
            try:
                self.guard_check()
                return True
            except (p.RelayError, OSError, ValueError):
                self.application_records.pop(operation.operation_id, None)
                if self.application_proofs:
                    self.application_proofs.forget(operation.operation_id)
                return False  # restoration cannot revive proof after observed trust loss
        deadline = asyncio.get_running_loop().time() + APPLICATION_OBSERVATION_SECONDS
        try:
            while once or asyncio.get_running_loop().time() < deadline:
                if self.peer.closed or self.owners.current is not owner or self.spec.run_id != operation.run_id:
                    return
                if not trusted():
                    return
                if hasattr(source, 'inspect_application'):
                    result = await source.inspect_application(value['decisionId'], value['actionDigest'], value['effect'], timeout=.01)
                else:
                    result = await source.applications.observe_answer(operation, value['effect'], timeout=.01)
                if result.get('outcome') == 'accepted':
                    if not trusted():
                        return
                    if self.owners.current is not owner or self.peer.closed or self.spec.run_id != operation.run_id:
                        return
                    application = result.get('application')
                    if not isinstance(application, dict):
                        return
                    if value['effect'] == 'deny' and application.get('nativeToolId'):
                        owner.confirm_denial(application['nativeToolId'])
                    await self.persist_application(value, result)
                    if not trusted() or self.peer.closed or self.owners.current is not owner or self.spec.run_id != operation.run_id:
                        return
                    acknowledged = await self.publish({'kind': 'decision.applied', 'operationId': operation.operation_id,
                                        'application': result['application']})
                    if acknowledged == {'outcome': 'accepted'}:
                        self.application_records.pop(operation.operation_id, None)
                    return
                if once:
                    return
                await asyncio.sleep(.1)
        except (p.RelayError, OSError, ValueError, ConnectionError, TimeoutError):
            return  # retained proof can be observed again, never another answer

    def check_writer(self, operation):
        if operation.actor.kind is not p.ActorKind.SERVICE:
            return super().check_writer(operation)
        from acc.session_relay.adapters.claude_agent import ClaudeAgentAdapter
        writer = self.delivery_writers.get(operation.operation_id)
        owner = self.controller.writer('chat') if self.controller else None
        if (operation.method != 'turn.submit' or self.spec.spec.get('auth_mode') != 'api_key'
                or writer is None or writer.kind is not p.ActorKind.PERSON
                or not isinstance(owner, ClaudeAgentAdapter) or owner.spec.run_id != operation.run_id):
            raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
        if (owner.background or owner.draining_background()
                or owner.active and self.active_operation != operation.operation_id):
            raise p.RelayError(p.ErrorCode.BUSY)

    def service_admitted(self, event):
        operation = self.service_operations.get(event['operationId'])
        if operation is None:
            return
        expected = self.adapter.attestation.process_identity + ':' + operation.operation_id
        if (event['payloadDigest'] != operation.payload_digest or event.get('nativeRef') != expected
                or self.adapter.attestation.run_id != operation.run_id):
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        result = AdmissionResult(Outcome.ACCEPTED, p.Evidence(p.EvidenceKind.PROVIDER_ADMISSION,
            operation.operation_id, operation.run_id, event['nativeRef']))
        self.journal.settle(operation, result)
        self.staging.remove(hashlib.sha256(operation.operation_id.encode()).hexdigest() + '.payload', settled=True)
        self.service_operations.pop(operation.operation_id)

    async def dispatch(self, operation):
        adapter = self.controller.writer('chat')
        if operation.method == 'turn.submit':
            self.active_operation = operation.operation_id
            if operation.actor.kind is p.ActorKind.SERVICE:
                if len(self.service_operations) >= 64:
                    raise p.RelayError(p.ErrorCode.LIMIT)
                self.service_operations[operation.operation_id] = operation
            try:
                return await adapter.submit(operation)
            except BaseException:
                self.service_operations.pop(operation.operation_id, None)
                raise
        if operation.method == 'turn.interrupt':
            from acc.session_relay.adapters.base import InterruptOutcome
            result = await adapter.interrupt(operation)
            # Exact control acknowledgement means requested, not cancelled.
            return AdmissionResult(Outcome.ACCEPTED, result.evidence) if result.outcome is InterruptOutcome.REQUESTED else AdmissionResult(Outcome.UNKNOWN)
        raise p.RelayError(p.ErrorCode.UNSUPPORTED)

    async def _close(self):
        # Reuse the base's single shielded cleanup task across Stop and EOF.
        for task in tuple(self.application_tasks):
            task.cancel()
        await asyncio.gather(*tuple(self.application_tasks), return_exceptions=True)
        self.application_records.clear()
        self.application_inflight.clear()
        self.service_operations.clear()
        try:
            if self.controller:
                await self.controller.close()
        finally:
            try:
                # A failed recovery can retain a partial factory separate from
                # the retired controller. Stop/EOF must reclaim that factory too.
                if self.owners:
                    await self.owners.close()
            finally:
                from .claude_denial_evidence import close
                try:
                    await close(self)
                finally:
                    try:
                        if self.application_proofs:
                            self.application_proofs.close()
                    finally:
                        journal = getattr(self, 'decision_evidence', None)
                        if journal is not None:
                            journal.close()
                        # Close every transport/resource, but an unproved old or
                        # partial owner still reserves its conversation identity.
                        self.retain_quarantine = False
                        for factory in (self.owners, getattr(self.controller, 'factory', None)):
                            self.retain_quarantine |= bool(getattr(factory, 'retirement_unproved', False))
                            exited = getattr(factory, 'exited', None)
                            if exited is not None:
                                try:
                                    self.retain_quarantine |= not await exited()
                                except (Exception, asyncio.CancelledError):
                                    self.retain_quarantine = True
                        await super()._close()
