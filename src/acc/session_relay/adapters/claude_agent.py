"""Claude Agent SDK seam. No credential files, permission bypass or alternate agent loop.

Capabilities stay unqualified until the installed SDK/CLI and hook policy have replay
and native qualification. The broker supplies a reviewed spec and a fresh policy
callback on every mutation; the adapter does not authorize a person itself.
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import asdict, is_dataclass, replace
from importlib.metadata import version
from pathlib import Path

from ..protocol import NO_CAPABILITIES, ActorKind, ErrorCode, RelayError
from .base import (
    AdmissionResult,
    CloseMode,
    ExitState,
    HistoryPage,
    HistoryState,
    InterruptOutcome,
    InterruptResult,
    Outcome,
    RuntimeAttestation,
    SwitchEvidence,
    UnavailableAdapter,
)


def sdk_client(options):
    # Optional deployment dependency. Importing the relay never launches a CLI.
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
    if version('claude-agent-sdk') != '0.1.50':
        raise RelayError(ErrorCode.VERSION_MISMATCH)
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

    from ..claude_permission_wire import PermissionWire
    configured=ClaudeAgentOptions(**options)
    async def empty_stream():
        return
        yield  # SDK streaming transport, no automatic user input
    native_options=replace(configured,permission_prompt_tool_name='stdio') if configured.can_use_tool else configured
    transport=PermissionWire(SubprocessCLITransport(prompt=empty_stream(),options=native_options))
    return ClaudeSDKClient(configured,transport=transport)


class ClaudeAgentAdapter(UnavailableAdapter):
    """One SDK connection, owned by one long-lived async task (SDK task-group constraint)."""

    def __init__(self, *, policy, publish, client_factory=sdk_client, clock=time.monotonic, history=None, history_follow=None, authorize=None, exit_proof=None):
        self.policy, self.publish = policy, publish
        self.authorize = authorize
        self.exit_proof = exit_proof
        self.factory, self.clock = client_factory, clock
        self.history, self.history_follow = history, history_follow
        self.spec = self.client = self.owner = self.attestation = None
        self.commands = asyncio.Queue()
        self.pending = {}
        self.pending_meta = {}
        from ..claude_sdk_application import SDKApplications
        self.applications = SDKApplications()
        self.native_decisions = {}  # exact bounded wire identity; never an application receipt
        self.answer_operations = {}
        self.completed_tools = set()
        self.failed_tools = set()
        self.seen = set()
        self.attempted = self.seen
        self.process = None
        self.exit_verified = False
        self.active = False
        self.background = set()
        self.background_drain = False
        self.native_tools = set()
        self.native_permissions = set()
        self.last_activity = clock()
        self.failure = False
        self.closing = False
        self.usage = {}
        self.response_usage = {}
        self.context_input_tokens = None

    def declaration(self):
        return replace(NO_CAPABILITIES, provider='claude', adapter_version='claude-agent-sdk/0.1.50',
                       schema_version='sdk-stdio-0.1.50', transport='stdio')

    def _check(self, operation=None):
        if self.spec is None or not self.policy(self.spec, operation):
            raise RelayError(ErrorCode.POLICY_REFUSED)
        # A live pipe is not a live supervised owner: a killed subreaper can
        # leave an orphan native child holding stdin/stdout. Fence commands and
        # incoming evidence on both sides of every authority await.
        if self.process is not None and self.process.returncode is not None:
            self.failure = True
            raise RelayError(ErrorCode.OFFLINE)
        if operation and (operation.run_id != self.spec.run_id or
                          operation.session_id != self.spec.spec['session_id']):
            raise RelayError(ErrorCode.STALE_RUN)

    async def _fresh(self, operation=None):
        self._check(operation)
        if self.authorize is not None and not await self.authorize(self.spec, operation):
            raise RelayError(ErrorCode.POLICY_REFUSED)
        self._check(operation)

    async def start(self, spec):
        if self.owner is not None:
            raise RelayError(ErrorCode.BUSY)
        self.spec = spec
        self._check()
        if spec.spec.get('permission_mode', 'default') not in ('default', 'acceptEdits', 'plan', 'bypassPermissions'):
            raise RelayError(ErrorCode.UNSUPPORTED)
        if (spec.binding.provider != 'claude' or not spec.spec.get('cli_path')
                or not spec.binding.native_conversation or spec.spec.get('prior_owner_exit_verified') is not True):
            raise RelayError(ErrorCode.INTEGRITY_FAILURE)
        # Reviewed launch must attest hook installation; do not suppress settings/hooks.
        if (spec.spec.get('guard_intact') is not True or
                not Path(spec.spec.get('settings_path', '')).is_absolute() or
                not Path(spec.spec['cli_path']).is_absolute() or
                spec.spec.get('checkpoint') or spec.spec.get('fork_session')):
            raise RelayError(ErrorCode.INTEGRITY_FAILURE)
        from ..claude_paths import validate
        validate(spec.spec,spec.spec.get('run_dir',str(Path(spec.spec['settings_path']).parent)))
        self.attestation = RuntimeAttestation(spec.binding.binding_id, spec.run_id,
                                              spec.binding.native_conversation, uuid.uuid4().hex)
        ready = asyncio.get_running_loop().create_future()
        self.owner = asyncio.create_task(self._own(ready))
        await ready
        return self.attestation

    async def _permission(self, tool, inputs, context):
        from claude_agent_sdk import PermissionResultDeny

        from ..claude_hook import TOOL_APPROVALS
        if tool not in TOOL_APPROVALS or tool in {'Glob','Grep'}:
            return PermissionResultDeny(message='This permission family is unavailable through the session hub.')
        await self._fresh()
        decision_id = uuid.uuid4().hex
        from ..claude_permission_wire import PermissionWire
        wire=getattr(self.client,'_transport',None)
        identity=wire.claim(tool,inputs) if isinstance(wire,PermissionWire) else None
        if identity is not None and len(self.native_decisions)<256:
            self.native_decisions[decision_id]=identity
        future = asyncio.get_running_loop().create_future()
        self.pending[decision_id] = future
        from ..protocol import canonical_json, sha256_digest
        digest = sha256_digest(canonical_json({'tool': tool, 'input': inputs}))
        deadline = time.monotonic() + 300
        expires = int(time.time() * 1000) + 300_000
        self.pending_meta[decision_id] = (digest, expires, deadline)
        async def expired():
            try:
                await self.publish({'kind':'decision.expired','decisionId':decision_id,'actionDigest':digest})
            except Exception:
                pass  # Losing main cannot turn expiry into a native allow.
            return PermissionResultDeny(message='This native decision expired.')
        try:
            self.applications.request(decision_id, tool, inputs)
            await self.publish({'kind': 'decision', 'id': decision_id, 'tool': tool, 'input': inputs,
                                'actionDigest': digest, 'expiresAtMs': expires})
            remaining = deadline-time.monotonic()
            if remaining <= 0 or int(time.time()*1000) >= expires:
                return await expired()
            try:
                answer = await asyncio.wait_for(future, remaining)
            except TimeoutError:
                return await expired()
            from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
            record = self.applications.records.get(decision_id)
            operation = record.get('answer') if record else None
            if operation is not None:
                expected = 'allow_once' if answer == 'allow' else 'deny'
                if record['effect'] != expected:
                    raise RelayError(ErrorCode.STALE_AUTHORITY)
                await self._fresh(operation)
            elif answer == 'allow':
                raise RelayError(ErrorCode.STALE_AUTHORITY)
            if answer == 'allow':
                if time.monotonic() >= deadline or int(time.time()*1000) >= expires:
                    return await expired()
                return PermissionResultAllow(updated_input=inputs)
            from ..claude_hook import denial_message
            return PermissionResultDeny(message=denial_message(decision_id))
        finally:
            if not future.done():
                future.cancel()
            self.pending.pop(decision_id, None)
            self.pending_meta.pop(decision_id, None)
            self.last_activity = self.clock()

    def options(self):
        """One reviewed option builder shared by launch and nonexecuting preview."""
        options = dict(cli_path=self.spec.spec.get('supervised_cli_path', self.spec.spec['cli_path']), cwd=self.spec.binding.workspace,
                       settings=self.spec.spec.get('sdk_settings_path', self.spec.spec['settings_path']),
                       system_prompt={'type': 'preset', 'preset': 'claude_code'},
                       setting_sources=[], permission_mode='default',
                       can_use_tool=self._permission)
        # Broker-reviewed parity only. Caller-supplied switches cannot reach this
        # spec. Unknown permission modes refuse rather than widening policy.
        mode = self.spec.spec.get('permission_mode', 'default')
        if mode not in ('default', 'acceptEdits', 'plan', 'bypassPermissions'):
            raise RelayError(ErrorCode.UNSUPPORTED)
        options['permission_mode'] = mode
        for key in ('model', 'effort'):
            if self.spec.spec.get(key):
                options[key] = self.spec.spec[key]
        if self.spec.spec.get('append_prompt_path'):
            options['extra_args'] = {'append-system-prompt-file': self.spec.spec['append_prompt_path']}
        if self.spec.spec.get('mcp_config_path'):
            options['mcp_servers'] = self.spec.spec['mcp_config_path']
            options.setdefault('extra_args', {})['strict-mcp-config'] = None
        if self.spec.spec.get('plugin_path'):
            options['plugins'] = [{'type': 'local', 'path': self.spec.spec['plugin_path']}]
        if 'launch_env' in self.spec.spec:
            # SDK 0.1.50 merges os.environ. Mask inherited values, then supply the
            # broker's minimal account environment. Never inherit a worker key.
            launch_env = dict(self.spec.spec['launch_env'])
            options['env'] = {**dict.fromkeys(os.environ, ''), **launch_env}
            # An inherited config root masked to '' is not absence to the CLI:
            # it can place SDK history under cwd, while the clean TUI uses HOME.
            # Pin the same reviewed default root on both ownership paths.
            if 'CLAUDE_CONFIG_DIR' not in launch_env:
                options['env']['CLAUDE_CONFIG_DIR'] = str(Path(launch_env['HOME']) / '.claude')
        if self.spec.spec.get('initial_chat'):
            options['extra_args'] = {**options.get('extra_args', {}), 'session-id': self.spec.binding.native_conversation}
        elif self.spec.binding.native_conversation:
            options['resume'] = self.spec.binding.native_conversation
        return options

    async def _own(self, ready):
        options = self.options()
        try:
            self.client = self.factory(options)
            try:
                await self.client.connect()
            finally:
                # A connect error may follow a successful fork. Preserve that
                # known owner's identity before disconnect clears the transport.
                self.process = getattr(getattr(self.client, '_transport', None), '_process', None)
                if self.process is not None:
                    started = Path(f'/proc/{self.process.pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
                    self.attestation = replace(self.attestation, process_identity=f'{self.process.pid}:{started}')
            # Pinned SDK 0.1.50 subprocess transport. Capture the process before
            # disconnect clears its private reference; unknown exit blocks L1.
            if self.process is None:
                raise RelayError(ErrorCode.INTEGRITY_FAILURE)
            # Pin the started owner before an authority recheck can refuse it;
            # failed-target cleanup still needs this process's own exit proof.
            await self._fresh()
            ready.set_result(None)
            while True:
                try:
                    command, payload, result = await asyncio.wait_for(self.commands.get(), 1)
                except TimeoutError:
                    if (not self.active and not self.pending and not self.background and not self.native_tools
                            and not self.native_permissions and not self.draining_background() and not self.failure
                            and self.clock() - self.last_activity >= 300):
                        break
                    continue
                try:
                    if command == 'stop':
                        result.set_result(None)
                        break
                    if command == 'query':
                        operation, text = payload
                        await self._fresh(operation)
                        self.applications.begin(operation, self.spec.binding.native_conversation)
                        native_start=len(getattr(getattr(self.client,'_transport',None),'results',()))
                        await self.client.query(text)
                        self.active = True
                        result.set_result(None)
                        # Receive separately so interrupt and person replies remain possible.
                        self.reader = asyncio.create_task(self._read(operation,native_start=native_start))
                        self.reader.add_done_callback(self._reader_done)
                    elif command == 'interrupt':
                        await self._fresh(payload)
                        from ..claude_permission_wire import PermissionWire
                        from ..claude_sdk_interrupt import scoped
                        wire = getattr(self.client, '_transport', None)
                        if not isinstance(wire, PermissionWire):
                            raise RelayError(ErrorCode.UNSUPPORTED)
                        async def final_interrupt():
                            await self._fresh(payload)
                            self._interrupt_target(payload)
                            if getattr(self.client, '_transport', None) is not wire or wire._process is not self.process:
                                raise RelayError(ErrorCode.STALE_AUTHORITY)
                            self.seen.add(payload.operation_id)  # subsequent write/ACK loss is uncertain, never unsent
                        self._interrupt_target(payload)
                        with scoped(wire, final_interrupt) as ticket:
                            await self.client.interrupt()
                        record = ticket['record']
                        evidence = None
                        outcome = InterruptOutcome.UNKNOWN
                        if record and record['writeCompleted'] and record['acknowledged'] and not record['ambiguous']:
                            from ..protocol import Evidence, EvidenceKind, canonical_json, sha256_digest
                            reference = sha256_digest(canonical_json([self.attestation.process_identity,
                                payload.operation_id, payload.run_id, payload.params['turnId'], record['requestId']]))
                            evidence = Evidence(EvidenceKind.PROVIDER_ADMISSION, payload.operation_id, payload.run_id, reference)
                            outcome = InterruptOutcome.REQUESTED
                        result.set_result(InterruptResult(outcome, payload.run_id, payload.params['turnId'], evidence))
                except Exception as error:
                    self.failure = True
                    result.set_exception(error)
        except Exception as error:
            self.failure = True
            if not ready.done():
                ready.set_exception(error)
        finally:
            self.closing = True
            for future in self.pending.values():
                if not future.done():
                    future.cancel()
            reader = getattr(self, 'reader', None)
            if reader and not reader.done():
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
            if self.client:
                await self.client.disconnect()
                self.exit_verified = self.process is not None and self.process.returncode is not None
                if self.exit_verified and self.exit_proof is not None:
                    self.exit_verified = await self.exit_proof(self.attestation)

    def _reader_done(self, task):
        if not task.cancelled() and task.exception() is not None:
            self.failure = True

    async def _read(self, operation=None, *, native_start=0):
        turn = self.applications.current
        response_done = False
        tools = {}; ambiguous = set()
        try:
            # receive_response stops at ResultMessage. Known background work
            # completes later on this same stream; keep its one reader alive.
            async for message in self.client.receive_messages():
                self._check(operation)
                self.applications.observe(turn, message)
                value = asdict(message) if is_dataclass(message) else dict(message)
                result = type(message).__name__ == 'ResultMessage'
                if result and response_done:
                    raise RelayError(ErrorCode.INTEGRITY_FAILURE)
                if type(message).__name__=='UserMessage' and isinstance(value.get('content'),list):
                    for block in value['content']:
                        if isinstance(block,dict) and isinstance(block.get('tool_use_id'),str):
                            identifier=block['tool_use_id']
                            if identifier in tools:ambiguous.add(identifier)
                            elif len(tools)<256:tools[identifier]=block.get('is_error')
                if value.get('subtype') == 'task_started':
                    self.background.add(value.get('task_id') or value.get('data', {}).get('task_id'))
                elif value.get('subtype') == 'task_notification':
                    self.background.discard(value.get('task_id') or value.get('data', {}).get('task_id'))
                native_id = value.get('session_id') or value.get('data', {}).get('session_id')
                if native_id and native_id != self.spec.binding.native_conversation:
                    raise RelayError(ErrorCode.INTEGRITY_FAILURE)
                # Publish through the hub's scoped/redacted history boundary, never directly to a client.
                await self.publish({'kind': 'provider_message', 'sdk_type': type(message).__name__, 'message': value})
                usage = value.get('usage')
                if usage:
                    for name, count in usage.items():
                        if type(count) is int:
                            previous = self.response_usage.get(name, 0)
                            observed = max(previous, count)
                            self.usage[name] = self.usage.get(name, 0) + observed - previous
                            self.response_usage[name] = observed
                            if name == 'input_tokens':
                                self.context_input_tokens = observed
                if result:
                    self.background_drain = bool(self.background)
                    response_done = True
                    self.active = False
                    if value.get('subtype') == 'success' and operation is not None and not self.failure:
                        from .. import claude_answer_evidence
                        from ..claude_permission_wire import PermissionWire
                        from ..protocol import canonical_json, sha256_digest
                        wire=getattr(self.client,'_transport',None)
                        if claude_answer_evidence.CLAUDE_ACK_QUALIFIED and isinstance(wire,PermissionWire) and len(wire.results)==native_start+1:
                            result=wire.results[native_start]
                            for identifier,(answer,effect) in tuple(self.answer_operations.items()):
                                identity=self.native_decisions.get(identifier)
                                if identity is None or answer.run_id!=self.spec.run_id or identity.tool_use_id in ambiguous:continue
                                behavior='allow' if effect=='allow_once' else 'deny'
                                if not claude_answer_evidence.applied(identity,wire.responses.get(identity.request_id),result,
                                        current=wire.current(identity),native_id=self.spec.binding.native_conversation,behavior=behavior,
                                        tool_seen=identity.tool_use_id in tools,tool_error=tools.get(identity.tool_use_id),post_tool=identity.tool_use_id in self.completed_tools,post_failed=identity.tool_use_id in self.failed_tools):continue
                                ref=sha256_digest(canonical_json([identity.request_id,identity.tool_use_id,result['uuid'],
                                    answer.run_id,answer.operation_id,effect]))
                                await self.publish({'kind':'decision.applied','decisionId':identifier,'operationId':answer.operation_id,
                                    'choiceId':answer.params['choiceId'],'actionDigest':answer.params['actionDigest'],
                                    'nativeRef':ref,'runId':answer.run_id})
                                self.answer_operations.pop(identifier,None)
                                if behavior=='deny':self.native_tools.discard(identity.tool_use_id)
                        await self.publish({'kind': 'operation.accepted', 'operationId': operation.operation_id,
                            'payloadDigest': operation.payload_digest,
                            'nativeRef': self.attestation.process_identity + ':' + operation.operation_id})
                        await self.publish({'kind': 'turn.completed', 'operationId': operation.operation_id})
                if response_done and not self.background:
                    break
        except Exception:
            self.failure = True
        finally:
            if not self.closing and (not response_done or self.background):
                self.failure = True  # stream loss is not background completion
            self.active = False
            self.background_drain = False
            self.last_activity = self.clock()

    def draining_background(self):
        reader = getattr(self, 'reader', None)
        return self.background_drain and reader is not None and not reader.done()

    async def _command(self, command, payload=None):
        if self.owner is None or self.owner.done() or (self.failure and command != 'stop'):
            raise RelayError(ErrorCode.OFFLINE)
        future = asyncio.get_running_loop().create_future()
        await self.commands.put((command, payload, future))
        return await asyncio.wait_for(asyncio.shield(future), 10)

    async def submit(self, operation):
        self._check(operation)
        if operation.method != 'turn.submit':
            raise RelayError(ErrorCode.UNSUPPORTED)
        if self.active or self.pending or self.background or self.draining_background():
            raise RelayError(ErrorCode.BUSY)
        if operation.operation_id in self.seen:
            return AdmissionResult(Outcome.UNKNOWN)
        text = operation.params.get('text')
        if not isinstance(text, str) or not text or len(text.encode('utf-8')) > 20000:
            raise RelayError(ErrorCode.LIMIT)
        if text.lstrip().startswith('/'):
            raise RelayError(ErrorCode.UNSUPPORTED)
        self.response_usage = {}
        self.seen.add(operation.operation_id)
        self.active = True  # fence simultaneous submits before awaiting native I/O
        try:
            await self._command('query', (operation, text))
        except Exception:
            self.failure = True
        # SDK query returning only proves a write, not provider admission.
        return AdmissionResult(Outcome.UNKNOWN)

    async def reply_verified(self, operation, effect):
        fields = operation.native_params({'decisionId', 'choiceId', 'expectedRevision', 'actionDigest'})
        if operation.actor.kind is not ActorKind.PERSON or effect not in ('allow_once', 'deny'):
            raise RelayError(ErrorCode.POLICY_REFUSED)
        meta = self.pending_meta.get(fields['decisionId'])
        if (not meta or meta[0] != fields['actionDigest'] or meta[1] <= int(time.time() * 1000)
                or meta[2] <= time.monotonic()):
            raise RelayError(ErrorCode.DECISION_RESOLVED)
        future = self.pending.get(fields['decisionId'])
        if future is None or future.done():
            raise RelayError(ErrorCode.DECISION_RESOLVED)
        await self._fresh(operation)
        if (self.pending.get(fields['decisionId']) is not future or future.done()
                or self.pending_meta.get(fields['decisionId']) != meta
                or meta[1] <= int(time.time()*1000) or meta[2] <= time.monotonic()):
            raise RelayError(ErrorCode.DECISION_RESOLVED)
        self.applications.bind(fields['decisionId'], operation, effect)
        future.set_result('allow' if effect == 'allow_once' else 'deny')
        if fields['decisionId'] in self.native_decisions and len(self.answer_operations)<256:
            self.answer_operations[fields['decisionId']]=(operation,effect)
        return AdmissionResult(Outcome.UNKNOWN)

    async def observe_application(self, operation, effect):
        return await self.applications.observe_answer(operation, effect)

    def confirm_denial(self, identifier):
        self.native_tools.discard(identifier)
        self.native_permissions.discard(identifier)

    async def reply_decision(self, operation):
        self._check(operation)
        if operation.method != 'decision.reply' or operation.actor.kind is not ActorKind.PERSON:
            raise RelayError(ErrorCode.POLICY_REFUSED)
        future = self.pending.get(operation.params.get('decision_id'))
        if future is None or future.done():
            raise RelayError(ErrorCode.DECISION_RESOLVED)
        choice = operation.params.get('choice')
        if choice not in ('allow', 'deny'):
            raise RelayError(ErrorCode.UNSUPPORTED)
        future.set_result(choice)
        return AdmissionResult(Outcome.UNKNOWN)

    def _interrupt_target(self, operation):
        turn = self.applications.current
        if (operation.actor.kind is not ActorKind.PERSON or not self.active or self.failure or turn is None or turn['failed']
                or turn['operation'].operation_id != operation.params.get('turnId')
                or turn['operation'].run_id != operation.run_id
                or turn['operation'].actor != operation.actor
                or self.reader is None or self.reader.done()):
            raise RelayError(ErrorCode.STALE_AUTHORITY)

    async def interrupt(self, operation):
        self._check(operation)
        if self.background or self.pending or self.native_permissions or self.draining_background():
            raise RelayError(ErrorCode.UNSUPPORTED, details={'reason': 'not_supported_in_release'})
        if operation.method != 'turn.interrupt':
            raise RelayError(ErrorCode.UNSUPPORTED)
        if operation.operation_id in self.seen:
            raise RelayError(ErrorCode.INTEGRITY_FAILURE)
        self._interrupt_target(operation)
        return await self._command('interrupt', operation)

    async def read_snapshot(self):
        # Caller supplies the existing scoped native transcript reader. No second history.
        if self.history and self.spec:
            return await self.history(self.spec.binding)
        return HistoryPage(HistoryState.UNSUPPORTED)

    async def follow(self, cursor=None):
        if self.history_follow is None or self.spec is None:
            raise RelayError(ErrorCode.UNSUPPORTED)
        async for page in self.history_follow(self.spec.binding, cursor):
            yield page

    async def reattach(self, attestation):
        if attestation != self.attestation or self.owner is None or self.owner.done():
            raise RelayError(ErrorCode.OFFLINE)
        return self.attestation

    async def reconcile(self, operation):
        self._check(operation)
        return AdmissionResult(Outcome.UNKNOWN)

    def observe_hook(self, payload):
        if payload.get('session_id') != self.spec.binding.native_conversation:
            self.failure = True
            raise RelayError(ErrorCode.STALE_RUN)
        event = payload.get('hook_event_name')
        identifier = payload.get('tool_use_id') or payload.get('_relay_decision_id')
        if event in ('PostToolUse', 'PostToolUseFailure'):
            from ..claude_hook import background_task_id
            try:
                background = background_task_id(payload)
            except ValueError:
                self.failure = True
                raise RelayError(ErrorCode.INTEGRITY_FAILURE) from None
            if background:
                if background not in self.background and len(self.background) >= 64:
                    self.failure = True
                    raise RelayError(ErrorCode.LIMIT)
                self.background.add(background)
        if event in ('SessionStart', 'SessionEnd', 'Notification', 'UserPromptSubmit', 'Stop'):
            return  # SDK result/active state, not these hooks, establishes idle.
        if not isinstance(identifier, str) or not 0 < len(identifier) <= 256:
            self.failure = True
            raise RelayError(ErrorCode.INTEGRITY_FAILURE)
        if event == 'PreToolUse':
            self.native_tools.add(identifier)
        elif event in ('PostToolUse', 'PostToolUseFailure') and identifier in self.native_tools:
            self.native_tools.remove(identifier)
            if event=='PostToolUse' and len(self.completed_tools)<256:self.completed_tools.add(identifier)
            if event=='PostToolUseFailure' and len(self.failed_tools)<256:self.failed_tools.add(identifier)
        elif event == 'PermissionRequest':
            self.native_permissions.add(identifier)
        elif event == 'PermissionDenied':
            self.native_tools.discard(identifier)
            self.native_permissions.discard(identifier)
        else:
            self.failure = True
        if max(len(self.native_tools), len(self.native_permissions)) > 64:
            self.failure = True
            raise RelayError(ErrorCode.LIMIT)

    def permission_closed(self, identifier):
        self.native_permissions.discard(identifier)

    async def prepare_switch(self):
        return SwitchEvidence(not self.active and not self.pending and not self.background and not self.native_tools and not self.native_permissions and not self.draining_background() and not self.failure,
                              ExitState.EXITED if self.exit_verified else ExitState.UNKNOWN if self.failure or (self.owner and self.owner.done()) else ExitState.RUNNING,
                              self.attestation.native_conversation if self.attestation else '',
                              self.spec.binding.launch_policy_digest if self.spec else '')

    async def stop_owner(self):
        if self.active or self.pending or self.background or self.native_tools or self.native_permissions or self.draining_background():
            raise RelayError(ErrorCode.BUSY)
        await self.close(CloseMode.STOP)
        return await self.prepare_switch()

    async def resume_owner(self, spec):
        if not self.exit_verified or not self.attestation:
            raise RelayError(ErrorCode.INTEGRITY_FAILURE)
        if (spec.run_id == self.attestation.run_id or
                spec.binding.native_conversation != self.attestation.native_conversation or
                any(getattr(spec.binding, name) != getattr(self.spec.binding, name)
                    for name in ('installation', 'node_enrollment', 'account', 'workspace'))):
            raise RelayError(ErrorCode.STALE_RUN)
        self.owner = None
        self.client = None
        self.process = None
        self.exit_verified = False
        self.failure = self.closing = False
        self.last_activity = self.clock()
        return await self.start(spec)

    async def idle_stop(self):
        if (self.active or self.pending or self.background or self.native_tools or self.native_permissions
                or self.draining_background() or self.failure or self.clock() - self.last_activity < 300):
            return False
        await self.close(CloseMode.STOP)
        return True

    async def close(self, mode):
        if mode is CloseMode.DETACH:
            return
        if self.owner and not self.owner.done():
            if not self.closing:
                await self._command('stop')
            await asyncio.wait_for(asyncio.shield(self.owner), 10)
