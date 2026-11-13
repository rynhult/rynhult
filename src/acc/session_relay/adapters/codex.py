"""Pinned Codex app-server adapter. Fixture coverage does not qualify a CLI.

Only reviewed new runs may start; attachment/reconciliation never launches or
replays. Raw provider data/errors never become UI history. The default record
qualifies nothing, including shared native Terminal.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import time

from ..codex_proxy import CodexNativeProxy
from ..protocol import (
    CAPABILITIES,
    MAX_FRAME_BYTES,
    MAX_PROMPT_BYTES,
    CapabilityDeclaration,
    CapabilityEntry,
    ErrorCode,
    Evidence,
    EvidenceKind,
    RelayError,
    Support,
    operation_digest,
)
from .base import (
    AdmissionResult,
    CloseMode,
    ConfigurationResult,
    FieldConfiguration,
    HistoryPage,
    HistoryState,
    InterruptOutcome,
    InterruptResult,
    Outcome,
    ProbeResult,
    ReviewedSpec,
    RuntimeAttestation,
    UnavailableAdapter,
)

ADAPTER_VERSION = '1'
NATIVE_SCHEMA = 'codex-app-server-r1-fixture-1'


class NativeRejection(Exception):
    """A correlated native error; text remains private."""


class NativeWriteRefused(RelayError):
    """The final native write barrier proved that zero request bytes were sent."""


class StdioRPC:
    """Bounded JSON-lines RPC. One reader; no retries or general native forwarding.

    Server requests are delivered to an injected private decision sink. They are
    never automatically approved or broadcast as writable native RPC messages.
    """
    def __init__(self, reader, writer, observe, *, timeout=15, capacity=64, lost=lambda: None):
        self.reader, self.writer, self.observe = reader, writer, observe
        self.timeout, self.capacity = timeout, capacity
        self.lost = lost
        self.ids = itertools.count(1)
        self.pending = {}
        self.closed = False
        self.eof = False
        self.write_lock = asyncio.Lock()
        self.task = asyncio.create_task(self._read())

    async def _read(self):
        try:
            while line := await self.reader.readline():
                if len(line) > MAX_FRAME_BYTES:
                    break
                message = json.loads(line)
                if not isinstance(message, dict):
                    break
                if 'method' in message:
                    await self.observe(message)
                elif type(message.get('id')) is int:
                    future = self.pending.get(message['id'])
                    if future and not future.done():
                        if 'error' in message:
                            future.set_exception(NativeRejection())
                        elif 'result' in message:
                            future.set_result(message['result'])
                        else:
                            break
            else:
                self.eof = True  # normal reader EOF, not parse failure/cancellation
        except (ValueError, OSError, ConnectionError):
            pass
        finally:
            self.closed = True
            self.lost()
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError('native connection lost'))

    async def request(self, method, params, *, before_write=None):
        if self.closed:
            raise ConnectionError('native connection closed')
        if len(self.pending) >= self.capacity:
            raise RelayError(ErrorCode.LIMIT)
        request_id = next(self.ids)
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self._write({'id': request_id, 'method': method, 'params': params}, before_write=before_write)
            return await asyncio.wait_for(future, self.timeout)
        finally:
            self.pending.pop(request_id, None)

    async def notify(self, method, params, *, before_write=None):
        await self._write({'method': method, 'params': params}, before_write=before_write)

    async def respond(self, request_id, result, *, before_write=None):
        await self._write({'id': request_id, 'result': result}, before_write=before_write)

    async def _write(self, message, *, before_write=None):
        data = json.dumps(message, separators=(',', ':')).encode() + b'\n'
        if len(data) > MAX_FRAME_BYTES:
            raise RelayError(ErrorCode.LIMIT)
        async with self.write_lock:
            if self.closed:
                raise ConnectionError('native connection closed')
            if before_write is not None:
                try:
                    if await before_write() is not True:
                        raise NativeWriteRefused(ErrorCode.STALE_AUTHORITY)
                except RelayError as error:
                    raise NativeWriteRefused(error.code) from None
                if self.closed:
                    raise NativeWriteRefused(ErrorCode.STALE_AUTHORITY)
            self.writer.write(data)
            await self.writer.drain()

    async def close(self):
        self.closed = True
        self.writer.close()
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)


def unqualified_declaration():
    return CapabilityDeclaration('codex', '', '', ADAPTER_VERSION, NATIVE_SCHEMA, 'stdio', '', '', {},
        {name: CapabilityEntry(Support.UNQUALIFIED, 'Native qualification required.') for name in CAPABILITIES})


class CodexAdapter(UnavailableAdapter):
    """runtime_factory/probe_native are broker-owned, never supplied by a client.

    runtime_factory returns an owner with request/notify/stop/attestation. The
    integration compiler must preserve the immutable policy, executable digest,
    private placement and guard configuration. No fallback to legacy input.
    """
    def __init__(self, runtime_factory, probe_native, *, declaration=None, authorize=None, clock=lambda: int(time.time() * 1000)):
        self.runtime_factory, self.probe_native = runtime_factory, probe_native
        self.record = declaration or unqualified_declaration()
        self.clock = clock
        self.authorize = authorize
        self.owner = self.attestation = self.binding = None
        self.active_turn = None
        self.last_admission = None
        self.session_id = None
        self.turns = {}
        self.decisions = {}
        self.file_items = {}
        from ..codex_answer_evidence import CommandAnswerEvidence
        self.answer_evidence = CommandAnswerEvidence()
        self.attempted = set()
        self.lock = asyncio.Lock()

    def declaration(self):
        return self.record

    def _qualified(self, capability):
        if (self.record.provider, self.record.adapter_version, self.record.schema_version, self.record.transport) != (
                'codex', ADAPTER_VERSION, NATIVE_SCHEMA, 'stdio') or self.record.support(capability) is not Support.QUALIFIED:
            raise RelayError(ErrorCode.UNSUPPORTED, details={'capability': capability})

    async def probe(self, binding):
        if binding.provider != 'codex':
            raise RelayError(ErrorCode.UNSUPPORTED)
        result: ProbeResult = await self.probe_native(binding)
        if (result.executable_version, result.executable_digest, result.protocol_version, result.transport) != (
                self.record.cli_version, self.record.cli_digest, NATIVE_SCHEMA, 'stdio') or not result.guard_intact:
            raise RelayError(ErrorCode.POLICY_REFUSED)
        return result

    async def start(self, spec: ReviewedSpec):
        async with self.lock:
            if self.owner is not None:
                raise RelayError(ErrorCode.BUSY)
            self._qualified('text')
            await self.probe(spec.binding)
            owner = await self.runtime_factory(spec)
            # Retain the owner even on failed initialization: never launch a replacement.
            self.owner, self.binding = owner, spec.binding
            self.session_id = spec.spec['session_id']
            await owner.request('initialize', {'clientInfo': {'name': 'rynhult', 'title': 'Rynhult', 'version': ADAPTER_VERSION}})
            await owner.notify('initialized', {})
            resumed = await owner.request('thread/resume', {'threadId': spec.binding.native_conversation})
            if not isinstance(resumed, dict) or resumed.get('thread', {}).get('id') != spec.binding.native_conversation:
                raise RelayError(ErrorCode.INTEGRITY_FAILURE)
            attestation = owner.attestation
            if (attestation.binding_id, attestation.run_id, attestation.native_conversation) != (
                    spec.binding.binding_id, spec.run_id, spec.binding.native_conversation):
                raise RelayError(ErrorCode.INTEGRITY_FAILURE)
            self.attestation = attestation
            return attestation

    async def reattach(self, attestation: RuntimeAttestation):
        self._qualified('reattach')
        if not self.attestation or attestation != self.attestation or not self.owner or not getattr(self.owner, 'alive', False):
            raise RelayError(ErrorCode.STALE_RUN)
        return self.attestation

    def _validate_operation(self, operation, method):
        if not self.attestation or operation.run_id != self.attestation.run_id or operation.session_id != self.session_id:
            raise RelayError(ErrorCode.STALE_RUN)
        if operation.method != method or operation.deadline_ms <= self.clock():
            raise RelayError(ErrorCode.STALE_AUTHORITY)
        if operation.payload_digest != operation_digest(operation.method, operation.params):
            raise RelayError(ErrorCode.INTEGRITY_FAILURE)

    def _operation(self, operation, method):
        self._validate_operation(operation, method)
        if operation.operation_id in self.attempted:
            raise RelayError(ErrorCode.INTEGRITY_FAILURE)
        # Claim in the durable broker journal precedes this in production. This
        # local set is an additional no-repeat guard, never restart durability.
        if len(self.attempted) >= 4096:
            raise RelayError(ErrorCode.LIMIT)
        self.attempted.add(operation.operation_id)

    async def _admission(self, operation, method):
        # Native probes may await. The broker's final authority must still be
        # current after them, immediately before the provider mutation.
        if self.authorize is not None and not await self.authorize(operation):
            raise RelayError(ErrorCode.STALE_AUTHORITY)
        self._operation(operation, method)

    async def _write_authority(self, operation, method, capability):
        if self.authorize is not None and not await self.authorize(operation):
            raise NativeWriteRefused(ErrorCode.STALE_AUTHORITY)
        self._qualified(capability)
        self._validate_operation(operation, method)
        if method == 'turn.interrupt' and (self.active_turn != operation.params['turnId']
                or operation.params['turnId'] not in self.turns):
            raise NativeWriteRefused(ErrorCode.STALE_AUTHORITY)
        return True

    async def _request(self, operation, method, params, capability):
        guarded = getattr(self.owner, 'request_guarded', None)
        if guarded is None:
            return await self.owner.request(method, params)  # synthetic adapter owners
        return await guarded(method, params, lambda: self._write_authority(operation, operation.method, capability))

    async def submit(self, operation):
        async with self.lock:
            self._qualified('text')
            params = operation.native_params({'text'}, optional={'expectedConfigurationRevision', 'expectedControlRevision', 'ifBusy'})
            if type(params.get('text')) is not str:
                raise RelayError(ErrorCode.UNSUPPORTED)
            text = params['text']
            if not text or len(text.encode()) > MAX_PROMPT_BYTES:
                raise RelayError(ErrorCode.LIMIT)
            if '\n' in text or '\r' in text:
                self._qualified('multiline')
            if self.active_turn is not None:
                raise RelayError(ErrorCode.BUSY)
            await self.probe(self.binding)
            await self._admission(operation, 'turn.submit')
            self.active_turn = operation.operation_id  # uncertain admission blocks subsequent turns too
            try:
                response = await self._request(operation, 'turn/start', {'threadId': self.attestation.native_conversation,
                    'input': [{'type': 'text', 'text': text, 'text_elements': []}]}, 'text')
                turn = response.get('turn') if isinstance(response, dict) else None
                # A plain RPC success is not admission: require exact native turn identity/status.
                if not isinstance(turn, dict) or type(turn.get('id')) is not str or not turn['id'] or turn.get('status') != 'inProgress':
                    return AdmissionResult(Outcome.UNKNOWN)
                self.active_turn = operation.operation_id
                self.turns[operation.operation_id] = turn['id']
                self.last_admission = (operation.operation_id, {'turn': turn})
                return AdmissionResult(Outcome.ACCEPTED, Evidence(EvidenceKind.PROVIDER_ADMISSION,
                    operation.operation_id, operation.run_id, turn['id']), operation.operation_id)
            except NativeRejection:
                self.active_turn = None
                return AdmissionResult(Outcome.REJECTED, Evidence(EvidenceKind.PROVIDER_REJECTION,
                    operation.operation_id, operation.run_id))
            except (OSError, ConnectionError, asyncio.TimeoutError):
                return AdmissionResult(Outcome.UNKNOWN)

    async def interrupt(self, operation):
        async with self.lock:
            self._qualified('interrupt')
            operation.native_params({'turnId'}, optional={'expectedControlRevision'})
            await self.probe(self.binding)
            await self._admission(operation, 'turn.interrupt')
            turn_id = operation.params['turnId']
            if turn_id != self.active_turn or turn_id not in self.turns:
                return InterruptResult(InterruptOutcome.NOT_ACTIVE, operation.run_id, turn_id,
                    Evidence(EvidenceKind.PROVIDER_REJECTION, operation.operation_id, operation.run_id))
            try:
                response = await self._request(operation, 'turn/interrupt', {'threadId': self.attestation.native_conversation,
                                                                     'turnId': self.turns[turn_id]}, 'interrupt')
                if response != {}:
                    return InterruptResult(InterruptOutcome.UNKNOWN, operation.run_id, turn_id)
                return InterruptResult(InterruptOutcome.REQUESTED, operation.run_id, turn_id,
                    Evidence(EvidenceKind.PROVIDER_ADMISSION, operation.operation_id, operation.run_id, self.turns[turn_id]))
            except NativeRejection:
                # A correlated RPC error proves rejection of this request, not
                # that the exact target turn has stopped or ceased to exist.
                # The native error schema supplies no qualified inactive proof.
                return InterruptResult(InterruptOutcome.UNKNOWN, operation.run_id, turn_id)
            except (OSError, ConnectionError, asyncio.TimeoutError):
                return InterruptResult(InterruptOutcome.UNKNOWN, operation.run_id, turn_id)

    def observe_file(self,event):
        params=event.get('params',{});item=params.get('item',{})
        if event.get('method')=='item/fileChange/patchUpdated':
            key=(params.get('threadId'),params.get('turnId'),params.get('itemId'))
            if key in self.file_items:self.file_items[key]=None
            return
        if event.get('method')!='item/started' or item.get('type')!='fileChange':return
        key=(params.get('threadId'),params.get('turnId'),item.get('id'))
        if key in self.file_items:self.file_items[key]=None
        elif len(self.file_items)<64:
            from ..native_approval import describe_file
            try:describe_file(item.get('changes'))
            except RelayError:self.file_items[key]=None
            else:self.file_items[key]=json.dumps(item['changes'],sort_keys=True)

    def file_review(self,request):
        params=request.get('params',{})
        if (params.keys()-{'threadId','turnId','itemId','reason','grantRoot','startedAtMs'}
                or params.get('grantRoot') is not None):
            raise RelayError(ErrorCode.UNSUPPORTED)
        raw=self.file_items.get((params.get('threadId'),params.get('turnId'),params.get('itemId')))
        changes=json.loads(raw) if raw is not None else None
        from ..native_approval import describe_file
        describe_file(changes,params.get('reason'))
        return changes

    def register_decision(self, decision_id, *, native_request, action_digest, revision, expires_ms):
        """Trusted hub callback after it stores an exact native request/action map.

        Not a client method. The hub owns authorization, expiry and winner CAS;
        this adapter adds a per-run, single-use native response fence.
        """
        from ..protocol import MAX_PENDING_DECISIONS, valid_request_id
        if not self.attestation or len(self.decisions) >= MAX_PENDING_DECISIONS:
            raise RelayError(ErrorCode.LIMIT)
        params = native_request.get('params', {})
        if native_request.get('method') not in {'item/commandExecution/requestApproval', 'item/fileChange/requestApproval'}:
            raise RelayError(ErrorCode.UNSUPPORTED)
        if not valid_request_id(native_request.get('id')) or params.get('threadId') != self.attestation.native_conversation \
                or params.get('turnId') != self.turns.get(self.active_turn) or not params.get('itemId'):
            raise RelayError(ErrorCode.INTEGRITY_FAILURE)

        if native_request['method']=='item/fileChange/requestApproval':
            self.file_review(native_request)
            if decision_id in self.decisions or any(v['native_id']==native_request['id'] for v in self.decisions.values()):
                raise RelayError(ErrorCode.DECISION_RESOLVED)
            self.decisions[decision_id]={'native_id':native_request['id'],'digest':action_digest,'revision':revision,
                'expires':expires_ms,'sent':False,'choices':frozenset({'accept','decline'}),'file_request':native_request,'file_review':self.file_review(native_request)}
            self.answer_evidence.register(decision_id,native_request,action_digest)
            return
        # Pinned 0.167.1 ordinary command callbacks include display metadata
        # and an offered policy amendment. Seeing that offer never authorizes it:
        # only the explicit one-time/cancel strings below can become hub choices.
        if params.keys() - {'threadId', 'turnId', 'itemId', 'command', 'cwd', 'reason', 'commandActions',
                            'availableDecisions', 'kind', 'startedAtMs', 'environmentId', 'proposedExecpolicyAmendment'}:
            raise RelayError(ErrorCode.UNSUPPORTED)
        if (params.get('kind', 'command') != 'command' or params.get('environmentId') not in (None, 'local')
                or ('startedAtMs' in params and (type(params['startedAtMs']) is not int or params['startedAtMs'] < 0))):
            raise RelayError(ErrorCode.UNSUPPORTED)
        proposal = params.get('proposedExecpolicyAmendment')
        if proposal is not None and (not isinstance(proposal, list) or len(proposal) > 64
                or any(type(part) is not str for part in proposal)):
            raise RelayError(ErrorCode.UNSUPPORTED)
        offered = params.get('availableDecisions')
        if offered is None:
            offered = ['accept', 'decline']
        if not isinstance(offered, list) or len(offered) > 64:
            raise RelayError(ErrorCode.UNSUPPORTED)
        choices = frozenset(value for value in offered
                            if type(value) is str and value in {'accept', 'decline', 'cancel'})
        if not choices:
            raise RelayError(ErrorCode.UNSUPPORTED)
        if decision_id in self.decisions or any(value['native_id'] == native_request['id'] for value in self.decisions.values()):
            raise RelayError(ErrorCode.DECISION_RESOLVED)
        self.decisions[decision_id] = {'native_id': native_request['id'], 'digest': action_digest,
            'revision': revision, 'expires': expires_ms, 'sent': False,
            'choices': choices}

        self.answer_evidence.register(decision_id,native_request,action_digest)

    async def reply_decision(self, operation):
        async with self.lock:
            self._qualified('decision.tool_approval')
            operation.native_params({'decisionId', 'choiceId', 'expectedRevision', 'actionDigest'})
            decision = self.decisions.get(operation.params['decisionId'])
            if decision is None or decision['sent']:
                raise RelayError(ErrorCode.DECISION_RESOLVED)
            if decision['expires'] <= self.clock():
                raise RelayError(ErrorCode.DECISION_EXPIRED)
            if (decision['revision'], decision['digest']) != (operation.params['expectedRevision'], operation.params['actionDigest']):
                raise RelayError(ErrorCode.REVISION_CONFLICT)
            choice = operation.params['choiceId']
            if choice not in decision['choices']:
                raise RelayError(ErrorCode.UNSUPPORTED)
            await self.probe(self.binding)
            if decision['expires'] <= self.clock():
                raise RelayError(ErrorCode.DECISION_EXPIRED)
            await self._admission(operation, 'decision.reply')
            if 'file_request' in decision and self.file_review(decision['file_request'])!=decision['file_review']:
                raise RelayError(ErrorCode.STALE_AUTHORITY)
            def final_reply_check():
                if (self.decisions.get(operation.params['decisionId']) is not decision
                        or operation.deadline_ms <= self.clock()):
                    raise RelayError(ErrorCode.STALE_AUTHORITY)
                if decision['expires'] <= self.clock():
                    raise RelayError(ErrorCode.DECISION_EXPIRED)
                if 'file_request' in decision and self.file_review(decision['file_request'])!=decision['file_review']:
                    raise RelayError(ErrorCode.STALE_AUTHORITY)
                self.answer_evidence.sending(operation.params['decisionId'],operation,choice)
                decision['sent'] = True
                return True
            async def before_reply_write():
                if self.authorize is not None and not await self.authorize(operation):
                    raise RelayError(ErrorCode.STALE_AUTHORITY)
                if (self.decisions.get(operation.params['decisionId']) is not decision
                        or operation.deadline_ms <= self.clock() or decision['expires'] <= self.clock()):
                    raise RelayError(ErrorCode.STALE_AUTHORITY)
            try:
                guarded = getattr(self.owner, 'respond_guarded', None)
                if guarded is None:
                    async def unguarded():
                        await before_reply_write()
                        return final_reply_check()
                    await self.owner.respond(decision['native_id'], {'decision': choice}, before_write=unguarded)
                else:
                    async def current():
                        await before_reply_write()
                        await self._write_authority(operation, 'decision.reply', 'decision.tool_approval')
                        return final_reply_check()
                    await guarded(decision['native_id'], {'decision': choice}, current)
            except (OSError, ConnectionError, asyncio.TimeoutError):
                pass
            # A response write or serverRequest/resolved (also emitted on cleanup)
            # cannot prove this answer applied. Keep the durable outcome unknown;
            # a qualified provider-specific reconciliation may later settle it.
            return AdmissionResult(Outcome.UNKNOWN)

    async def read_snapshot(self):
        # Reuse A's scoped/redacted history seam when integrated. Never expose raw native items.
        return HistoryPage(HistoryState.UNSUPPORTED)

    async def read_configuration(self):
        return ConfigurationResult(FieldConfiguration(), FieldConfiguration())

    async def native_client_proxy(self, binding):
        if binding != self.binding:
            raise RelayError(ErrorCode.NOT_FOUND_OR_DENIED)
        return CodexNativeProxy(lambda: (), None)  # native correlation not qualified

    async def reconcile(self, operation):
        # Durable journal is authoritative. No payload retry or speculative native lookup.
        return AdmissionResult(Outcome.UNKNOWN)

    async def close(self, mode: CloseMode):
        if mode is CloseMode.STOP and self.owner:
            await self.owner.stop()
        # DETACH leaves the broker's owner and protocol reader running.
