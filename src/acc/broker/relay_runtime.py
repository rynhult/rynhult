"""Production relay composition over the authenticated account broker channel."""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import os
import pwd
import secrets
import shutil
import stat
import time
import uuid
from dataclasses import replace
from pathlib import Path

from acc.env import Environment, interpreter_variable
from acc.session_relay import protocol as p
from acc.session_relay.adapters.base import (
    AdapterOperation,
    AdmissionResult,
    Outcome,
    ProbeResult,
    ReviewedSpec,
)
from acc.session_relay.adapters.codex import ADAPTER_VERSION, NATIVE_SCHEMA, CodexAdapter, unqualified_declaration
from acc.session_relay.runtime_peer import RuntimePeer

from . import ops
from .relay_tui import OwnedTUI
from .session_relay import AdmissionBroker, AdmissionJournal, CodexRuntimeOwner, PayloadStaging, private_dir

CLI_VERSION = '0.167.1'
CLI_DIGEST = 'sha256:dd5bfdf19d794dcc6fb482d2d5a2d9fb31d11f6caa44b303bb72890d461e8a02'


def operation_from(value):
    return AdapterOperation(value['operationId'], value['sessionId'], value['runId'], value['method'],
                            value['payloadDigest'], p.Actor(p.ActorKind(value['actor']['kind']),
                            value['actor']['principal'], p.Channel(value['actor']['channel']), value['actor'].get('device')),
                            p.Fence(**value['fence']), value['deadlineMs'], value['params'])


def operation_wire(operation):
    return {**p.to_wire(operation), 'params': p.to_wire(operation.params), 'fence': {
        'authority_epoch': operation.fence.authority_epoch, 'broker_incarnation': operation.fence.broker_incarnation,
        'hub_incarnation': operation.fence.hub_incarnation}}


class RuntimeTransportFactory:
    """No runtime on construction/ping/read; only a reviewed launch can enroll.

    Main resolves opaque tokens on the authenticated stream. Neither an operation
    nor an actor can be supplied in the broker dispatch request. EOF immediately
    fences pending admission; the journal preserves admitted uncertain outcomes.
    """
    version = 2
    def __init__(self, config, *, current_config=None, owned_view_factory=OwnedTUI):
        self.owned_view_factory = owned_view_factory
        self.initial_config = config
        self.current_config = current_config
        self.active = {}
        self.lock = asyncio.Lock()

    @property
    def config(self):
        return self.current_config() if self.current_config else self.initial_config

    def probe(self, provider='codex'):
        if provider == 'claude':
            from .claude_owner import CLI_DIGEST as CLAUDE_CLI_DIGEST
            from .claude_owner import installation_probe
            return {**installation_probe(self.config), 'provider': 'claude', 'cliDigest': CLAUDE_CLI_DIGEST}
        if provider != 'codex':
            return {'available': False}
        if self.owned_view_factory and not self.owned_view_factory.available():
            return {'available': False}
        executable = shutil.which(self.config.providers.get('codex', ''))
        if not executable:
            return {'available': False}
        path = Path(executable).resolve()
        if path.suffix == '.js':
            matches = list(path.parent.parent.glob('node_modules/@openai/codex-*/vendor/*/bin/codex'))
            if len(matches) != 1:
                return {'available': False}
            path = matches[0]
        return {'available': p.sha256_digest(path.read_bytes()) == CLI_DIGEST}

    async def serve(self, request_id, params, reader, writer):
        if (params.keys() != {'version', 'enrollment'} or type(params['version']) is not int
                or params['version'] not in (2, 3, 4, 5) or type(params['enrollment']) is not str):
            writer.write(json.dumps({'id': request_id, 'ok': False, 'error': 'Runtime enrollment refused'}).encode() + b'\n')
            await writer.drain()
            writer.close()
            return
        writer.write(json.dumps({'id': request_id, 'ok': True, 'result': {'version': params['version']}}).encode() + b'\n')
        await writer.drain()
        if params['version'] == 4:
            from .application_reader import ApplicationReader
            state = ApplicationReader(self, params['enrollment'])
        elif params['version'] == 5:
            from .claude_reconciliation import ReadOnlyEvidence
            state = ReadOnlyEvidence(self, params['enrollment'])
        elif params['version'] == 3:
            from .relay_ownership import ExclusiveRuntime
            state = ExclusiveRuntime(self, params['enrollment'])
        else:
            state = BrokerRuntime(self, params['enrollment'])
        peer = RuntimePeer(reader, writer, state.handle, lost=state.lost)
        state.peer = peer
        try:
            await peer.task
        finally:
            await peer.close()
            await state.close()


class BrokerRuntime:
    def __init__(self, factory, enrollment):
        self.factory, self.config, self.enrollment = factory, factory.config, enrollment
        self.peer = self.broker = self.journal = self.staging = self.adapter = self.owner = None
        self.binding = self.spec = None
        self.input_frame = contextvars.ContextVar('relay_input_frame', default=None)
        self.link_id = os.urandom(16).hex()
        self.events = asyncio.Queue(maxsize=64)
        self.event_task = None
        self.publication_failed = False
        self.exit_task = None
        self.native_loss_task = None
        self.final_exit_task = None
        self.key = None
        self.ready = False
        self.stopping = False
        self.operation_lock = asyncio.Lock()
        self.guards = {}
        self.writers = None
        self.writer_grant = None
        self.delivery_writers = {}
        self.native_socket = None
        self.terminal_host = None
        self.owned_view = None
        self.owned_generations = []  # bounded, retained read-only native clients/Hosts
        self.native_start = None
        self.operation_origin = {}
        self.cleanup_task = None

    def lost(self):
        if self.writers is not None:
            self.writers.close()
        if self.broker is not None:
            self.broker.link = None

    def check_owner_lifetime(self):
        lifetime = getattr(getattr(self.owner, 'runtime', None), 'kernel_lifetime', None)
        if lifetime is not None and not lifetime.alive():
            self.lost()
            self.ready = False
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)

    def guard_check(self):
        config = self.factory.config
        if config.folder_policy_required and not config.folder_revision:
            raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
        ops._under_roots(config, self.binding.workspace)
        for name, expected in self.guards.items():
            path = self.run_dir / name
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or info.st_mode & 0o222 or info.st_size > p.MAX_FRAME_BYTES):
                    raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
                if p.sha256_digest(os.read(fd, p.MAX_FRAME_BYTES + 1)) != expected:
                    raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            finally:
                os.close(fd)

    def decision_view_available(self, operation):
        if operation.method != 'decision.reply' or self.owned_view is None:
            return True
        if self.operation_origin.get(operation.operation_id, 'chat') == 'terminal':
            return self.owned_view.bootstrapped()
        return getattr(self.owned_view, 'alive', self.owned_view.bootstrapped)()

    async def verify(self, operation):
        self.check_owner_lifetime()
        if not self.ready or self.peer.closed or self.broker.link != (operation.fence, self.link_id):
            return False
        self.guard_check()
        if not self.decision_view_available(operation):
            return False
        self.check_writer(operation)
        try:
            value = await self.peer.call('authority.verify', {'grant': self.grant, 'operation': {**operation_wire(operation),
                'origin': self.operation_origin.get(operation.operation_id, 'chat')}})
        except (ConnectionError, TimeoutError):
            return False
        self.check_owner_lifetime()
        self.guard_check()
        if not self.decision_view_available(operation):
            return False
        self.check_writer(operation)
        return value is True and not self.peer.closed and self.broker.link == (operation.fence, self.link_id)

    def check_writer(self, operation):
        if operation.actor.kind is p.ActorKind.SERVICE and (self.writers is None or operation.method != 'turn.submit'):
            raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
        if self.writers is not None and operation.method in ('turn.submit', 'turn.interrupt'):
            lease = self.writers.gate.lease
            actor = operation.actor
            if actor.kind is p.ActorKind.SERVICE:
                actor = self.delivery_writers.get(operation.operation_id)
                if actor is None or actor.kind is not p.ActorKind.PERSON:
                    raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            self.writers.gate.admit(lease, actor=actor, view=self.operation_origin.get(operation.operation_id, 'chat'))
            if type(operation.params.get('expectedControlRevision')) is not int or operation.params.get('expectedControlRevision') != lease.epoch:
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)

    async def authorize_writer(self, actor):
        self.check_owner_lifetime()
        allowed = await self.peer.call('authority.writer', {'actor': p.to_wire(actor), 'grant': self.writer_grant}) is True
        self.check_owner_lifetime()
        return allowed

    def bind_terminal_host(self, opener, native_peer, *, alive=None):
        """Internal owned-view composition; no RPC/read/attach can supply these.

        The caller must own/verify the native process and normal framed opener.
        There is no default view launcher or qualification/attestation promotion.
        """
        from acc.session_relay.adapters.base import TerminalRun
        from acc.session_relay.native_input_host import NativeInputHost
        if (not self.ready or self.writers is None or self.native_socket is None
                or (self.terminal_host is not None and not self.terminal_host.retired)):
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        async def validate(grant):
            try:
                frame = self.input_frame.get()
                action, sequence = ('input', frame[1]) if frame and frame[0] == grant.grant else ('held', None)
                return (await self.terminal_value(grant.grant, action, sequence) == grant
                        and (alive is None or alive() is True))
            except (ConnectionError, TimeoutError, p.RelayError):
                return False
        async def no_geometry(*_):
            return False  # geometry needs its separate qualified grant
        async def read(run):
            return run == TerminalRun(self.spec.spec['session_id'], self.spec.run_id) and await self.native_authorize()
        from .stock_approval_runtime import service as stock_approvals
        host = NativeInputHost(TerminalRun(self.spec.spec['session_id'], self.spec.run_id), opener,
            validate, no_geometry, validate_read=read, routes=self.writers,
            idle=self.native_idle, processed=self.native_processed, approval_bridge=self.peer.call, approval_ready=stock_approvals(self).ready)
        self.terminal_host = host
        def route(websocket):
            for view, prior in self.owned_generations:
                if view.accepts(websocket) is True:
                    return prior
            return host if native_peer(websocket) is True else None
        self.native_socket.writer_route = route
        self.writers.retire = self.retire_writer
        return host

    async def native_idle(self):
        rpc = getattr(self.owner, 'rpc', None)
        if rpc is None or rpc.closed or rpc.pending or self.adapter.active_turn is not None:
            return False
        if any(not value.get('sent') for value in self.adapter.decisions.values()):
            return False
        # Idle is fresh native metadata, not full-history hydration (which is
        # unavailable before the first genuine user message).
        value = await self.owner.request('thread/read', {'threadId': self.binding.native_conversation, 'includeTurns': False})
        return (value.get('thread', {}).get('id') == self.binding.native_conversation
                and value['thread'].get('status', {}).get('type') == 'idle'
                and not rpc.pending and self.adapter.active_turn is None)

    async def retire_writer(self, lease):
        if self.terminal_host is None:
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        if self.owned_view is None or not self.factory.owned_view_factory:
            return await self.terminal_host.retire(lease)
        if len(self.owned_generations) >= 16:
            raise p.RelayError(p.ErrorCode.LIMIT)
        previous, host = self.owned_view, self.terminal_host
        proof = await host.retire(lease)
        # Detach only its input bridge. The client, draft and read subscription
        # remain alive; this Host and its captured RPC route never become writable.
        await host.detach(host.run)
        root = self.run_dir / 'relay' / ('view-' + str(len(self.owned_generations)))
        view = self.factory.owned_view_factory(root, self.config, self.spec,
            previous.executable, self.native_socket, digest=previous.digest)
        self.owned_view = view
        fresh = self.bind_terminal_host(view.open, view.accepts, alive=view.bootstrapped)
        self.owned_generations.append((view, fresh))  # cleanup owns even failed starts
        await view.start(self.native_authorize)  # read-only; gate rechecks prospective writer separately
        if (not host.retired or fresh.draft or fresh.ticket or fresh.grant is not None
                or view.peer == previous.peer or view.name == previous.name):
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        return replace(proof, retired_client=previous.name, fresh_client=view.name, draft_retained=True, draft_retained_readonly=True, new_input_empty=True)

    async def native_admit(self, actor, method, params):
        host = self.terminal_host
        ticket = getattr(host, 'ticket', None)
        if (method != 'turn.submit' or ticket is None or not ticket.inflight
                or ticket.grant.actor != actor or params != {'text': ticket.text, 'expectedControlRevision': ticket.grant.epoch}):
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        await self.terminal_value(ticket.grant.grant, 'held')
        receipt = await self.peer.call('native.submit', {'grant': ticket.grant.grant, 'sequence': ticket.sequence,
                                                       'text': ticket.text, 'revision': ticket.grant.epoch})
        await self.terminal_value(ticket.grant.grant, 'held')
        if (receipt.get('actor') != p.to_wire(actor) or receipt.get('runId') != self.spec.run_id
                or receipt.get('method') != 'turn.submit'):
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        if receipt.get('state') == 'accepted':
            cached = self.adapter.last_admission
            if cached is None or cached[0] != receipt.get('operationId'):
                raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            return {**cached[1], 'state': 'accepted', 'operationId': receipt['operationId']}
        return {'state': receipt['state'], 'operationId': receipt['operationId']}

    async def native_processed(self, grant, sequence, result):
        return await self.peer.call('native.processed', {'grant': grant.grant, 'sequence': sequence,
                                                       'operationId': result['operationId']}) is True

    async def handle(self, method, params):
        if method == 'launch' and params == {} and not self.ready and self.adapter is None:
            async with self.factory.lock:
                if self.key is not None:
                    raise ValueError('Launch already attempted')
                return await self.launch()
        if not self.ready or self.peer.closed:
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        if method == 'receipt.read':
            from .relay_receipt_read import read
            return await read(self, params)
        if method == 'terminal.approval_withdraw' and params.keys() == {'alias', 'decisionId', 'winningOperationId'}:
            from .stock_approval_runtime import service as stock_approvals
            return await stock_approvals(self).withdraw(params)
        if method == 'terminal.approval_processed' and params.keys() == {'grant', 'sequence', 'operationId'}:
            if self.terminal_host is None:
                raise p.RelayError(p.ErrorCode.UNSUPPORTED)
            return await self.terminal_host.approval_input.completed(params['grant'], params['sequence'], params['operationId'])
        if method == 'terminal.read':
            from .relay_terminal_read import read
            return await read(self, params)
        if method in ('terminal.claim', 'terminal.input'):
            return await self.terminal_request(method, params)
        if method == 'writer.take' and params.keys() == {'grant'} and self.writers is not None:
            async with self.operation_lock:
                self.writer_grant = params['grant']
                try:
                    value = await self.peer.call('authority.resolve', params)
                    operation = operation_from(value)
                    if operation.method not in ('control.take', 'writer.take') or value['receiptState'] != 'submitting':
                        raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
                    await self.writers.take(operation.actor, operation.params['view'])
                    return {'view': self.writers.gate.lease.view, 'epoch': self.writers.gate.epoch,
                            'runId': self.spec.run_id, 'executionIdentity': self.writers.gate.execution_identity}
                finally:
                    self.writer_grant = None
        if method == 'stage' and params.keys() == {'grant'}:
            async with self.operation_lock:
                return await self.stage_grant(params['grant'])
        if method == 'dispatch' and params.keys() == {'grant'}:
            async with self.operation_lock:
                return await self.dispatch_grant(params['grant'])
        if method == 'discard' and params.keys() == {'grant'}:
            async with self.operation_lock:
                value = await self.peer.call('authority.discard', params)
                if self.journal.lookup(value['operationId']) is not None:
                    raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
                try:
                    self.staging.remove(value['reference'], settled=True)
                except FileNotFoundError:
                    pass
                return True
        if method == 'receipt' and params.keys() == {'grant'}:
            value = await self.peer.call('authority.resolve', params)
            operation = operation_from(value)
            return self.journal.lookup(operation.operation_id)
        if method == 'stop' and params == {}:
            if await self.peer.call('authority.stop', {}) is not True:
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            async with self.broker.lock:
                self.broker.link = None
                self.stopping = True
            # All retained views and the producer must retire before ACK. Do
            # not hold admission lock while detaching Host/native readers.
            await self.close()
            views = [view for view, _host in self.owned_generations]
            if self.owned_view is not None and self.owned_view not in views:
                views.append(self.owned_view)
            return {'exited': (self.owner.runtime.exit_proven is True
                               and all(view.exit_proven is True for view in views)),
                    'wholeTree': 'enclosure-v1'}
        raise p.RelayError(p.ErrorCode.UNSUPPORTED)

    async def terminal_value(self, token, action, sequence=None):
        from acc.session_relay.adapters.base import InputGrant, TerminalRun
        self.check_owner_lifetime()
        value = await self.peer.call('authority.terminal', {'grant': token, 'action': action, 'sequence': sequence})
        self.check_owner_lifetime()
        actor = value['actor']
        fence = value['fence']
        grant = InputGrant(TerminalRun(value['run']['sessionId'], value['run']['runId']), value['attachmentId'],
            p.Actor(p.ActorKind(actor['kind']), actor['principal'], p.Channel(actor['channel']), actor.get('device')),
            value['grant'], value['epoch'], p.Fence(fence['authorityEpoch'], fence['brokerIncarnation'], fence['hubIncarnation']))
        if (grant.grant != token or grant.run != TerminalRun(self.spec.spec['session_id'], self.spec.run_id)
                or grant.fence != self.broker.link[0] or self.writers is None):
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        lease = self.writers.gate.lease
        self.writers.gate.admit(lease, actor=grant.actor, view='terminal')
        if lease.epoch != grant.epoch:
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        return grant

    async def terminal_request(self, method, params):
        import base64
        if self.terminal_host is None:
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        expected = {'grant'} if method == 'terminal.claim' else {'grant', 'sequence', 'data'}
        if params.keys() != expected:
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        from acc.session_relay.native_input_host import NativeInputHost
        if method == 'terminal.input' and isinstance(self.terminal_host, NativeInputHost):
            # This Host validates once inside its final write lock, AFTER opening
            # the transport, including the exact main-reserved frame sequence.
            # A cached grant is an identity, never cached authorization. Main
            # reauthenticates the HTTP caller again after the returned await.
            grant = self.terminal_host.grant
            if grant is None or not secrets.compare_digest(params['grant'], grant.grant):
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            token = self.input_frame.set((params['grant'], params['sequence']))
            try:
                return p.to_wire(await self.terminal_host.write_frames(grant, params['sequence'],
                    base64.b64decode(params['data'], validate=True)))
            finally:
                self.input_frame.reset(token)
        action = 'claim' if method == 'terminal.claim' else 'input'
        sequence = params.get('sequence')
        grant = await self.terminal_value(params['grant'], action, sequence)
        if method == 'terminal.claim':
            await self.terminal_host.claim_input(grant)
            result = {'claimed': True, 'epoch': grant.epoch}
        else:
            result = p.to_wire(await self.terminal_host.write_frames(grant, sequence, base64.b64decode(params['data'], validate=True)))
        if await self.terminal_value(params['grant'], action, sequence) != grant:
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        return result

    async def stage_grant(self, grant):
        value = await self.peer.call('authority.resolve', {'grant': grant})
        operation = operation_from(value)
        payload = p.canonical_json(p.to_wire(operation.params))
        if operation.payload_digest != p.operation_digest(operation.method, operation.params):
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        try:
            reference, digest = self.staging.stage(operation.operation_id, payload)
        except FileExistsError:
            import hashlib
            reference = hashlib.sha256(operation.operation_id.encode()).hexdigest() + '.payload'
            digest = p.sha256_digest(payload)
        if self.staging.read(reference, digest) != payload:
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        await self.peer.call('authority.staged', {'grant': grant, 'reference': reference, 'digest': digest})
        return {'staged': True}

    async def dispatch_grant(self, grant):
        value = await self.peer.call('authority.resolve', {'grant': grant})
        staging = value['staging']
        payload = self.staging.read(staging['reference'], staging['digest'])
        operation = operation_from({**value, 'params': json.loads(payload)})
        self.grant = grant
        origin = value.get('origin')
        if origin not in ('chat', 'terminal'):
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        writer = value.get('deliveryWriter')
        if operation.actor.kind is p.ActorKind.SERVICE:
            if (operation.method != 'turn.submit' or origin != 'chat' or not isinstance(writer, dict)
                    or writer.get('kind') != 'person'):
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            self.delivery_writers[operation.operation_id] = p.Actor(p.ActorKind(writer['kind']), writer['principal'],
                p.Channel(writer['channel']), writer.get('device'))
        elif writer is not None:
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        self.operation_origin[operation.operation_id] = origin
        try:
            if (not self.broker.link or operation.fence != self.broker.link[0]
                    or operation.session_id != self.spec.spec['session_id']
                    or operation.payload_digest != p.operation_digest(operation.method, operation.params)):
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            result = await self.broker.dispatch(operation, self.dispatch)
        except p.RelayError as error:
            from acc.session_relay.adapters.codex import NativeWriteRefused
            claimed = self.journal.lookup(operation.operation_id)
            if claimed is not None and operation.operation_id in self.adapter.attempted and not isinstance(error, NativeWriteRefused):
                result = AdmissionResult(Outcome.UNKNOWN)
            else:
                result = AdmissionResult(Outcome.REJECTED, p.Evidence(p.EvidenceKind.BROKER_REFUSAL,
                                         operation.operation_id, operation.run_id))
                if claimed is not None:
                    self.journal.settle(operation, result)
        finally:
            self.operation_origin.pop(operation.operation_id, None)
            self.delivery_writers.pop(operation.operation_id, None)
        if result.outcome is not Outcome.UNKNOWN:
            self.staging.remove(staging['reference'], settled=True)
        return p.to_wire(result)

    async def dispatch(self, operation):
        if operation.method == 'turn.interrupt':
            from acc.session_relay.adapters.base import InterruptOutcome
            result = await self.adapter.interrupt(operation)
            if result.outcome in (InterruptOutcome.REQUESTED, InterruptOutcome.CONFIRMED):
                return AdmissionResult(Outcome.ACCEPTED, result.evidence)
            if result.outcome is InterruptOutcome.NOT_ACTIVE:
                return AdmissionResult(Outcome.REJECTED, result.evidence)
            return AdmissionResult(Outcome.UNKNOWN)
        methods = {'turn.submit': self.adapter.submit, 'decision.reply': self.adapter.reply_decision}
        if operation.method not in methods:
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        return await methods[operation.method](operation)

    async def launch(self):
        if self.factory.owned_view_factory and not self.factory.owned_view_factory.available():
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        value = await self.peer.call('authority.enroll', {'enrollment': self.enrollment, 'linkId': self.link_id})
        self.spec = ReviewedSpec(p.Binding(**value['binding']), value['runId'], value['spec'])
        self.binding = self.spec.binding
        self.key = (self.spec.spec['session_id'], self.spec.run_id)
        if self.key in self.factory.active or len(self.factory.active) >= 2048:
            raise p.RelayError(p.ErrorCode.LIMIT)
        self.factory.active[self.key] = self
        if (self.binding.provider != 'codex' or self.binding.account != pwd.getpwuid(os.getuid()).pw_name
                or self.spec.spec.get('new_session') is not True or self.binding.native_conversation):
            raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
        cwd = ops._under_roots(self.config, self.binding.workspace)
        self.run_dir = self.config.run_dir / self.spec.spec['session_id']
        if self.spec.spec.get('run_dir') != str(self.run_dir):
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        if not ops.ACC_ID.fullmatch(self.spec.spec['session_id']):
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        files, env = value['files'], value['env']
        if any(not ops.ENV_KEY.fullmatch(k) or k in ops.RESERVED_ENV or interpreter_variable(k)
               or type(v) is not str for k, v in env.items()):
            raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
        self.guards = {name: p.sha256_digest(files[name].encode()) for name in ops.GUARD_FILES if name in files}
        if 'guardrails.json' not in self.guards or p.sha256_digest(p.canonical_json(self.guards)) != self.binding.launch_policy_digest:
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        ops._materialize_run(self.run_dir, files, env)
        self.guard_check()
        executable = shutil.which(self.config.providers['codex'])
        if not executable:
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        # Production pin is on the native executable, not the npm launcher.
        resolved = Path(executable).resolve()
        if resolved.suffix == '.js':
            matches = list(resolved.parent.parent.glob('node_modules/@openai/codex-*/vendor/*/bin/codex'))
            if len(matches) != 1:
                raise p.RelayError(p.ErrorCode.UNSUPPORTED)
            resolved = matches[0]
        if p.sha256_digest(resolved.read_bytes()) != CLI_DIGEST:
            raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
        providers_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        from acc import providers
        providers.get('codex').bootstrap(providers_home, self.config.hook_path, exe=str(resolved))
        guarded = ' '.join(f'{name}={digest.removeprefix("sha256:")}' for name, digest in self.guards.items())
        environment = {**env, **dict(Environment({'ACC_RUN_DIR': str(self.run_dir),
                       'ACC_SESSION_ID': self.spec.spec['session_id'], 'ACC_GUARD_FILES': guarded})),
                       'HOME': str(providers_home), 'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8'}
        root = self.run_dir / 'relay'
        os.close(private_dir(root))
        self.journal = AdmissionJournal(root / 'journal')
        self.broker = AdmissionBroker(self.journal, self.verify)
        self.staging = PayloadStaging(root / 'payloads')
        async def observe(event):
            try:
                self.events.put_nowait(event)
            except asyncio.QueueFull:
                self.peer.invalidate()
        # The launch is rechecked after preparation, immediately before spawn.
        await self.peer.call('authority.launch_check', {})
        self.owner = await CodexRuntimeOwner.start(root / 'owners', self.spec, str(resolved), environment, observe,
            lost=self.native_lost)
        await self.peer.call('authority.launch_check', {})
        native_initialized = await self.owner.request('initialize', {'clientInfo': {'name': 'rynhult', 'version': ADAPTER_VERSION},
            'capabilities': {'experimentalApi': True}})
        await self.owner.notify('initialized', {})
        await self.peer.call('authority.launch_check', {})
        # The pinned build advertises paginated history before its cold storage
        # path supports every page operation. Freeze the supported legacy contract
        # at creation; native clients cannot select or change it.
        thread = await self.owner.request('thread/start', {'cwd': str(cwd), 'ephemeral': False, 'historyMode': 'legacy'})
        from .relay_sandbox import check
        check(thread.get('sandbox'),cwd,self.run_dir/'relay')
        self.native_start = thread
        native = thread.get('thread', {}).get('id')
        if not p.valid_opaque(native):
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        self.binding = replace(self.binding, native_conversation=native)
        self.owner.attestation = replace(self.owner.attestation, native_conversation=native)
        response = await self.peer.call('authority.launched', {'nativeId': native,
            'processIdentity': self.owner.attestation.process_identity, 'brokerIncarnation': self.broker.incarnation})
        self.spec = replace(self.spec, binding=self.binding)
        record = replace(unqualified_declaration(), cli_version=CLI_VERSION, cli_digest=CLI_DIGEST)
        async def probe(_binding):
            self.guard_check()
            return ProbeResult(CLI_VERSION, CLI_DIGEST, NATIVE_SCHEMA, 'stdio', True, frozenset())
        self.adapter = CodexAdapter(None, probe, declaration=record, authorize=self.verify)
        self.adapter.owner, self.adapter.binding = self.owner, self.binding
        self.adapter.attestation, self.adapter.session_id = self.owner.attestation, self.key[0]
        fence = p.Fence(response['authorityEpoch'], self.broker.incarnation, response['hubIncarnation'])
        await self.broker.enroll(fence, self.link_id)
        self.ready = True
        if response.get('writerActor'):
            from acc.session_relay.writer_routes import WriterRoutes
            self.writers = WriterRoutes(run_id=self.spec.run_id,
                execution_identity=self.owner.attestation.process_identity,
                authorize=self.authorize_writer, retire=self.retire_writer,
                admit=self.native_admit, read=self.writer_read)
            await self.writers.take(p.Actor(p.ActorKind(response['writerActor']['kind']), response['writerActor']['principal'],
                p.Channel(response['writerActor']['channel']), response['writerActor'].get('device')), 'chat')
        from acc.session_relay.native_socket import NativeSocket
        self.native_socket = NativeSocket(root, native, self.native_read, self.native_authorize,
                                          initialized=native_initialized, start_settings=self.native_start)
        await self.native_socket.start()
        if self.factory.owned_view_factory:
            if self.writers is None:
                raise p.RelayError(p.ErrorCode.UNSUPPORTED)
            self.owned_view = self.factory.owned_view_factory(root / 'view', self.config, self.spec,
                                                             resolved, self.native_socket, digest=CLI_DIGEST)
            self.bind_terminal_host(self.owned_view.open, self.owned_view.accepts, alive=self.owned_view.bootstrapped)
            self.owned_generations.append((self.owned_view, self.terminal_host))
            actor = p.Actor(p.ActorKind(response['writerActor']['kind']), response['writerActor']['principal'],
                            p.Channel(response['writerActor']['channel']), response['writerActor'].get('device'))
            await self.owned_view.start(lambda: self.authorize_writer(actor))
        self.event_task = asyncio.create_task(self.publish())
        self.event_task.add_done_callback(self.publication_done)
        self.exit_task = asyncio.create_task(self.observe_exit())
        return {'runId': self.spec.run_id, 'bindingId': self.binding.binding_id, 'capabilities': {}}

    async def native_authorize(self):
        try:
            return await self.peer.call('authority.native_read', {}) is True
        except (ConnectionError, TimeoutError):
            return False

    async def writer_read(self, method, params):
        from acc.session_relay.native_socket import READS, SCOPED
        if method not in READS or (method in SCOPED and params.get('threadId') != self.binding.native_conversation):
            raise p.RelayError(p.ErrorCode.STALE_RUN)
        if not await self.native_authorize():
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        result = await self.native_read(method, params)
        if not await self.native_authorize():
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        return result

    async def native_read(self, method, params):
        if params.get('cursor') is not None:
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        # A genuine newly-created thread has no materialized history before its
        # first message. Never hide a failed/unknown turn behind this snapshot.
        pristine = self.native_start.get('thread') if self.native_start else None
        if (method in ('thread/read', 'thread/resume', 'thread/turns/list') and getattr(self.owner, 'turn_attempted', True) is False
                and isinstance(pristine, dict) and pristine.get('id') == self.binding.native_conversation
                and pristine.get('turns') == []):
            if method == 'thread/read':
                return {'thread': dict(pristine)}
            if method == 'thread/resume':
                return {**self.native_start, 'thread': dict(pristine)}
            return {'data': [], 'nextCursor': None, 'backwardsCursor': None}
        if method == 'thread/items/list':
            value = await self.owner.request('thread/read', {'threadId': self.binding.native_conversation, 'includeTurns': True})
            thread = value.get('thread', {})
            turns = [turn for turn in thread.get('turns', []) if turn.get('id') == params.get('turnId')]
            if thread.get('id') != self.binding.native_conversation or len(turns) != 1:
                raise p.RelayError(p.ErrorCode.STALE_RUN)
            items = turns[0].get('items')
            if not isinstance(items, list) or len(items) > (params.get('limit') or 100):
                raise p.RelayError(p.ErrorCode.UNSUPPORTED)
            return {'data': list(reversed(items)) if params.get('sortDirection') == 'desc' else items,
                'nextCursor': None, 'backwardsCursor': None}
        if method == 'account/read':
            return {'account': None, 'requiresOpenaiAuth': False}
        if method == 'config/read':
            from acc.session_relay.native_socket import CONFIG_FIELDS
            value = await self.owner.request(method, {'includeLayers': False})
            return {'config': {key: value['config'][key] for key in CONFIG_FIELDS if key in value['config']}, 'origins': {}}
        if method == 'thread/resume':
            # Project this already-owned thread, never reconfigure/resume a new
            # execution owner on the app-server for a read-only native client.
            value = await self.owner.request('thread/read', {'threadId': self.binding.native_conversation, 'includeTurns': True})
            if (self.native_start is None or value.get('thread', {}).get('id') != self.binding.native_conversation):
                raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            # The pinned resume response requires the frozen start settings as
            # well as current history. A read must never issue native resume.
            return {**self.native_start, 'thread': value['thread']}
        return await self.owner.request(method, params)

    def publication_done(self, task):
        if not task.cancelled() and task.exception() is not None:
            self.peer.invalidate()

    def native_lost(self):
        if self.stopping:
            return
        # The stdio channel may close just before waitpid reports exit. Fence
        # every mutation immediately, but keep the private authority stream long
        # enough to publish conclusive supervised exit evidence when available.
        self.lost()
        self.ready = False
        if self.owner is None:
            self.peer.invalidate()
        elif self.native_loss_task is None:
            self.native_loss_task = asyncio.create_task(self.finish_native_loss())
            self.native_loss_task.add_done_callback(self.publication_done)

    async def finish_native_loss(self):
        try:
            code = await asyncio.wait_for(self.owner.runtime.process.wait(), 1)
        except (TimeoutError, OSError):
            self.peer.invalidate()  # EOF alone proves neither exit nor status.
            return
        await self.finish_exit(code)

    async def observe_exit(self):
        from acc.session_relay.kernel_lifetime import wait_root_exit
        code = await wait_root_exit(self.owner.runtime)
        if not self.stopping:
            self.lost()
            self.ready = False
            await self.finish_exit(code)
    async def finish_exit(self, code):
        native_status = getattr(self.owner.runtime, 'native_exit_status', None)
        if native_status is not None:
            code = native_status()  # supervisor status is not the vendor outcome
        if self.final_exit_task is None:
            self.final_exit_task = asyncio.create_task(self.publish_exit(code))
            self.final_exit_task.add_done_callback(self.publication_done)
        await asyncio.shield(self.final_exit_task)

    async def drain_final_events(self):
        rpc = getattr(self.owner, 'rpc', None)
        if rpc is None:
            return False
        try:
            # waitpid can race the stdio reader's last buffered native events.
            # A normal reader EOF must precede queue drain acknowledgement.
            await asyncio.wait_for(asyncio.shield(rpc.task), 1)
            await asyncio.wait_for(self.events.join(), 3)
        except (OSError, TimeoutError):
            return False
        return rpc.eof is True and not self.publication_failed

    async def publish_exit(self, code):
        self.lost()
        self.ready = False
        screen = None
        try:
            from acc.session_relay.adapters.base import TerminalRun

            from .relay_terminal_read import capture_owned
            try:
                self.guard_check()
                candidate = await asyncio.wait_for(capture_owned(self,
                    TerminalRun(self.spec.spec['session_id'], self.spec.run_id)), 2)
                self.guard_check()
                screen = candidate
            except (p.RelayError, OSError, TimeoutError):
                pass  # no final view proof: retain an explicit unavailable result
            drained = await self.drain_final_events()
            await self.peer.call('publication', {'kind': 'exited', 'returnCode': code,
                'finalScreen': screen, 'finalEventsDrained': drained})
        finally:
            self.peer.invalidate()

    async def publish(self):
        while True:
            event = await self.events.get()
            try:
                await self.publish_event(event)
            except BaseException:
                self.publication_failed = True
                raise
            finally:
                self.events.task_done()

    async def publish_event(self, event):
        params = event.get('params', {})
        if self.native_socket is not None:
            await self.native_socket.publish(event.get('method'), params)
        if params.get('threadId') != self.binding.native_conversation:
            return
        async with self.adapter.lock:
            self.adapter.observe_file(event)
            applied = self.adapter.answer_evidence.observe(event)
        for proof in applied:
            await self.peer.call('publication', proof)
        if event.get('method') == 'turn/completed':
            native = params.get('turn', {}).get('id')
            matches = [key for key, value in self.adapter.turns.items() if value == native]
            if len(matches) == 1:
                if self.adapter.active_turn == matches[0]:
                    self.adapter.active_turn = None
                outcome = params.get('turn', {}).get('status')
                if outcome not in ('completed', 'interrupted', 'failed'):
                    raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
                await self.peer.call('publication', {'kind': 'turn.' + outcome, 'operationId': matches[0]})
        elif event.get('method') == 'item/completed':
            item = params.get('item', {})
            identifier = item.get('id')
            if params.get('turnId') is not None:
                identifier = hashlib.sha256(p.canonical_json([params['turnId'], identifier])).hexdigest()
            if item.get('type') == 'agentMessage' and type(item.get('text')) is str:
                await self.peer.call('publication', {'kind': 'message', 'itemId': identifier, 'text': item['text']})
            elif item.get('type') == 'userMessage':
                content = item.get('content')
                if (not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], dict)
                        or content[0].get('type') != 'text' or type(content[0].get('text')) is not str):
                    raise p.RelayError(p.ErrorCode.UNSUPPORTED)
                await self.peer.call('publication', {'kind': 'message', 'role': 'user',
                    'itemId': identifier, 'text': content[0]['text']})
        elif 'id' in event:
            async with self.adapter.lock:
                identifier = uuid.uuid4().hex
                digest = p.sha256_digest(p.canonical_json(params))
                if event.get('method') == 'item/fileChange/requestApproval':
                    changes = self.adapter.file_review(event)
                    digest = p.sha256_digest(p.canonical_json({'request': params, 'changes': changes}))
                expires = int(time.time() * 1000) + 300_000
                self.adapter.register_decision(identifier, native_request=event,
                    action_digest=digest, revision=0, expires_ms=expires)
                from acc.session_relay.native_approval import describe_command
                if event.get('method') == 'item/fileChange/requestApproval':
                    from acc.session_relay.native_approval import describe_file
                    description = describe_file(changes, params.get('reason'))
                else:
                    description = describe_command(params)
                await self.peer.call('publication', {'kind': 'decision', 'decisionId': identifier,
                    'actionDigest': digest, 'expiresAtMs': expires, 'description': description,
                    'choices': sorted(self.adapter.decisions[identifier]['choices'])})
            from .stock_approval_runtime import service as stock_approvals
            await stock_approvals(self).queued(identifier, event, digest)

    async def close(self):
        self.lost()
        # EOF/Stop/caller cancellation can overlap. Resource close is one owned
        # operation; cancelling one waiter cannot strand an execution owner.
        if self.cleanup_task is None:
            self.cleanup_task = asyncio.create_task(self._close())
        await asyncio.shield(self.cleanup_task)

    async def _close(self):
        self.stopping = True  # cleanup must not create a fresh EOF/finalize task
        errors = []
        async def attempt(action):
            try:
                await action()
            except (Exception, asyncio.CancelledError) as error:
                errors.append(error)
        for task in (self.event_task, self.exit_task, self.native_loss_task, self.final_exit_task):
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        hosts = [host for _view, host in self.owned_generations]
        if self.terminal_host is not None and self.terminal_host not in hosts:
            hosts.append(self.terminal_host)
        for host in hosts:
            await attempt(lambda host=host: host.detach(host.run))
        views = [view for view, _host in self.owned_generations]
        if self.owned_view is not None and self.owned_view not in views:
            views.append(self.owned_view)
        for view in views:
            async def close_view(view=view):
                if await view.close() is False:
                    raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            await attempt(close_view)
        if self.native_socket:
            await attempt(self.native_socket.close)
        # A failed attachment/view cleanup must not strand a writable owner.
        if self.owner:
            await attempt(self.owner.stop)
        for resource in (self.staging, self.journal):
            if resource:
                try:
                    resource.close()
                except Exception as error:
                    errors.append(error)
        if (self.key is not None and self.factory.active.get(self.key) is self
                and not getattr(self, 'retain_quarantine', False)):
            self.factory.active.pop(self.key, None)
        if errors:
            raise errors[0]  # cleanup failure remains visible after all releases
