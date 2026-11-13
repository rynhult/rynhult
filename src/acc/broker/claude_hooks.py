"""Private account-local native hooks. Actor authority stays in main's hub."""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets
import socket
import time

from acc import peercred
from acc.session_relay import protocol as p
from acc.session_relay.approval_facts import native_facts
from acc.session_relay.claude_hook import TOOL_APPROVALS

from .session_relay import private_dir


class ClaudeHookServer:
    def __init__(self, directory, spec, authorize, publish, observe, permission_closed=None, prepare_application=None, guard=None, *, accept_pid=None, sdk_background=None, sdk_permission_owner=None, allow_policy=True):
        self.accept_pid=accept_pid
        self.directory, self.spec = directory, spec
        self.authorize, self.publish, self.observe = authorize, publish, observe
        self.permission_closed = permission_closed
        self.prepare_application = prepare_application
        self.guard = guard
        self.sdk_background = sdk_background
        self.sdk_permission_owner = sdk_permission_owner
        self.allow_policy = allow_policy is True
        self.server = None
        self.token = secrets.token_hex(32)
        self.pending = {}
        self.tool_requests = {}
        self.applications = {}
        self.handlers = set()
        self.identity = None
        self.fd = None
        from .claude_tool_binding import ToolBindings
        self.tool_bindings = ToolBindings()
        self.evidence_journal = None

    async def start(self):
        self.fd = private_dir(self.directory)
        info = os.fstat(self.fd)
        self.identity = info.st_dev, info.st_ino
        path = self.directory / 'native-hook.sock'
        if path.exists() or path.is_symlink():
            raise p.RelayError(p.ErrorCode.BUSY)
        self.server = await asyncio.start_unix_server(self.connection, f'/proc/self/fd/{self.fd}/native-hook.sock', limit=p.MAX_FRAME_BYTES)
        path.chmod(0o600)
        channel = self.directory / 'owner-channel.json'
        fd = os.open(channel, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, p.canonical_json({'token': self.token, 'runId': self.spec.run_id,
                                         'nativeId': self.spec.binding.native_conversation}))
            os.fsync(fd)
        finally:
            os.close(fd)

    async def connection(self, reader, writer):
        task = asyncio.current_task()
        if len(self.handlers) >= 16:
            writer.close()
            return
        self.handlers.add(task)
        reply = {'behavior': 'deny'}
        identifier, answer_verified = None, False
        try:
            info = self.directory.stat(follow_symlinks=False)
            if ((info.st_dev, info.st_ino) != self.identity or info.st_mode & 0o077
                    or peercred.peer_uid(writer.get_extra_info('socket')) != os.getuid()):
                raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            sock=writer.get_extra_info('socket')
            pid=peercred.UCRED.unpack(sock.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,peercred.UCRED.size))[0]
            if self.accept_pid is None or not self.accept_pid(pid):
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            raw = await asyncio.wait_for(reader.readline(), 3)
            request = p.decode_frame(raw)
            if (request.keys() != {'token', 'runId', 'payload'} or not isinstance(request['token'],str) or not hmac.compare_digest(request['token'],self.token)
                    or request['runId'] != self.spec.run_id or not isinstance(request['payload'], dict)
                    or request['payload'].get('session_id') != self.spec.binding.native_conversation):
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            payload = request['payload']
            try:
                authorized = await self.authorize()
            except Exception:
                # Main refusal/transport loss is not new execution authority.
                # The already authenticated channel may still supply evidence
                # for an original sent record; never discard it as an answer.
                authorized = False
            if not authorized:
                # A revoked person's grant cannot authorize another answer.
                # Hook traffic cannot journal completion after authority ends.
                # Separately authenticated durable reconciliation grants no input.
                if payload.get('hook_event_name') in ('Stop','SubagentStop','SessionEnd'):
                    reply = {'observed': True}
                    return  # no owner idle, publication or input acknowledgement
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            if payload.get('hook_event_name') == 'PreToolUse':
                if self.guard is not None and (not await self.guard(payload) or not await self.authorize()):
                    return  # explicit deny; never add refused work to the owner ledger
                inputs = payload.get('tool_input')
                # Only the exact owned SDK has the qualified task-notification
                # reader. TUI and unknown owners still refuse before execution.
                if (isinstance(inputs, dict) and inputs.get('run_in_background') is not None
                        and inputs.get('run_in_background') is not False):
                    if (inputs['run_in_background'] is not True or self.sdk_background is None
                            or self.sdk_background() is not True):
                        raise p.RelayError(p.ErrorCode.UNSUPPORTED)
            event = payload.get('hook_event_name')
            tool_id, prompt_id = payload.get('tool_use_id'), payload.get('prompt_id')
            action = p.sha256_digest(p.canonical_json({'tool': payload.get('tool_name'), 'input': payload.get('tool_input')}))
            if event == 'PreToolUse' and p.valid_opaque(tool_id) and p.valid_opaque(prompt_id):
                if tool_id in self.tool_requests or len(self.tool_requests) >= 64:
                    raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
                self.tool_requests[tool_id] = {'prompt': prompt_id, 'action': action, 'completed': False, 'decision': None}
            elif event in ('PostToolUse', 'PostToolUseFailure'):
                entry = self.tool_requests.get(tool_id)
                if entry and entry['prompt'] == prompt_id and entry['action'] == action:
                    entry['completed'] = event == 'PostToolUse'
                    if entry['decision'] is None:
                        self.tool_requests.pop(tool_id)
            if (payload.get('hook_event_name') == 'PermissionRequest' and self.sdk_permission_owner is not None
                    and self.sdk_permission_owner() is True):
                # The owned SDK stdio callback is the decision authority. An
                # additional native hook card would race/deny that same tool.
                # This delegates no allow and is never enabled for a TUI owner.
                if not await self.authorize() or not self.token or self.sdk_permission_owner() is not True:
                    raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
                reply = {'sdkPermissionOwner': True}
            elif payload.get('hook_event_name') == 'PermissionRequest':
                if payload.get('tool_name') not in TOOL_APPROVALS:
                    raise p.RelayError(p.ErrorCode.UNSUPPORTED)
                if len(self.pending) >= 16:
                    raise p.RelayError(p.ErrorCode.LIMIT)
                # One socket invocation is an exact pending request, even when
                # the vendor supplies no tool-use ID. Never guess coalescing.
                identifier = secrets.token_hex(16)
                tool_id = payload.get('tool_use_id')
                await self.observe({**payload, '_relay_decision_id': identifier})
                digest = p.sha256_digest(p.canonical_json({'tool': payload.get('tool_name'),
                    'input': payload.get('tool_input'), 'nativeToolId': tool_id}))
                future = asyncio.get_running_loop().create_future()
                deadline = time.monotonic() + 15
                expires_at_ms = time.time_ns() // 1_000_000 + 15_000
                candidates = [key for key, entry in self.tool_requests.items() if entry['prompt'] == prompt_id
                    and entry['action'] == action and not entry['completed'] and entry['decision'] is None]
                bound_tool = candidates[0] if len(candidates) == 1 else None
                if bound_tool is not None:
                    self.tool_requests[bound_tool]['decision'] = identifier
                prepared = self.prepare_application({**payload, '_relay_native_tool_id': bound_tool}, identifier) if self.prepare_application else None
                application = prepared.get('deny') if isinstance(prepared, dict) else prepared
                allow_application = prepared.get('allow_once') if isinstance(prepared, dict) else None
                self.pending[identifier] = {'future': future, 'digest': digest, 'nativeToolId': tool_id,
                                            'claimed': False, 'verify_answer': None, 'deadline': deadline, 'promptId': payload.get('prompt_id'), 'toolName': payload.get('tool_name'), 'application': application, 'allow_application': allow_application,
                                            'managed_bash_allow': isinstance(prepared, dict) and prepared.get('managed_bash_allow') is True}
                from .claude_tool_binding import prompt_digest
                self.pending[identifier].update(promptDigest=prompt_digest(payload), boundToolId=self.tool_bindings.bind(payload))
                async def expired():
                    if self.pending[identifier]['claimed']:
                        return  # A chosen unknown answer is not an unanswered expiration.
                    try:
                        await self.publish({'kind':'decision.expired','decisionId':identifier,'actionDigest':digest})
                    except Exception:
                        pass  # Loss of main authority still returns an explicit native deny.
                try:
                    if not await self.authorize():
                        raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
                    from acc.session_relay.native_approval import describe_tool
                    description = describe_tool(payload.get('tool_name'), payload.get('tool_input'),
                        getattr(self.spec.binding, 'workspace', None))
                    allow = self.allow_permitted(self.pending[identifier])
                    if description and not allow:
                        description += '\nAllow is unavailable for this permission policy.'
                    await self.publish({'kind': 'decision', 'decisionId': identifier, 'actionDigest': digest,
                        'expiresAtMs': expires_at_ms, 'description': description, 'approval': native_facts(
                            payload.get('tool_name'), payload.get('tool_input'), payload.get('cwd'),
                            source='native_hook', native_tool_id=tool_id),
                        'choices': ['accept', 'decline'] if description and allow else ['decline']})
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        await expired()
                        behavior = 'deny'
                    else:
                        try:
                            behavior = await asyncio.wait_for(future, remaining)
                        except TimeoutError:
                            await expired()
                            behavior = 'deny'
                    pending = self.pending.get(identifier)
                    verify_answer = pending.get('verify_answer') if pending else None
                    answer_verified = (verify_answer is not None and await self.authorize()
                        and await verify_answer() and time.monotonic() < deadline)
                    if not answer_verified:
                        behavior = 'deny'
                    if behavior == 'allow' and (not await self.authorize() or not self.token or time.monotonic() >= deadline):
                        behavior = 'deny'
                    if behavior == 'allow':
                        if not self.allow_permitted(self.pending[identifier]):
                            behavior = 'deny'
                        else:
                            if self.evidence_journal is not None:
                                self.evidence_journal.sent(vars(self.spec.binding),
                                    self.pending[identifier]['boundToolId'], self.pending[identifier]['promptDigest'])
                            self.tool_bindings.allow_sent(self.pending[identifier])
                    reply = {'behavior': behavior}
                    reference = self.pending[identifier].get('denyReference')
                    if behavior == 'deny' and reference is not None and answer_verified:
                        reply['denyReference'] = reference
                    if behavior == 'deny' and application is not None and reference is None and answer_verified:
                        reply['applicationMarker'] = identifier
                finally:
                    if not future.done():
                        future.cancel()
                    self.pending.pop(identifier, None)
                    if self.permission_closed:
                        self.permission_closed(tool_id or identifier)
            else:
                await self.observe(payload)
                if not await self.authorize():
                    raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
                self.tool_bindings.observe(payload)
                if self.evidence_journal is not None:
                    self.evidence_journal.observe(vars(self.spec.binding), payload)
                reply = {'observed': True}
        except (Exception, asyncio.CancelledError):
            # Explicit deny even on cancellation; never return no decision and
            # thereby fall through to an unmanaged native permission prompt.
            pass
        finally:
            try:
                writer.write(json.dumps(reply).encode() + b'\n')
                record = self.applications.get(identifier) if identifier is not None else None
                if record is not None and answer_verified:
                    record['reply_written'] = True  # only a prerequisite, never provider evidence
                if record is not None:
                    record['reply_done'].set()
                await writer.drain()
            except (OSError, ConnectionError):
                pass
            writer.close()
            self.handlers.discard(task)

    def allow_permitted(self, pending):
        # The separately proven managed Bash profile has an exact registered
        # input and native application observer. Unbound/file-tool ask policies
        # retain the conservative fence, and every path needs a unique native ID.
        managed = (pending.get('managed_bash_allow') is True and pending.get('toolName') == 'Bash'
                   and pending.get('allow_application') is not None)
        return (self.allow_policy or managed) and self.tool_bindings.permits(pending, self.pending)

    async def answer(self, identifier, digest, effect, verify, *, deny_reference=None):
        pending = self.pending.get(identifier)
        if (not pending or pending['claimed'] or pending['deadline'] <= time.monotonic() or pending['digest'] != digest
                or effect not in ('allow_once', 'deny')):
            raise p.RelayError(p.ErrorCode.DECISION_RESOLVED)
        if effect == 'allow_once' and (not self.allow_permitted(pending)):
            raise p.RelayError(p.ErrorCode.INPUT_CONFLICT)
        if not await verify():
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        # Recheck after the awaited main verifier, then consume before signal.
        if (self.pending.get(identifier) is not pending or pending['claimed'] or pending['future'].done()
                or pending['deadline'] <= time.monotonic()):
            raise p.RelayError(p.ErrorCode.DECISION_RESOLVED)
        if effect == 'allow_once' and (not self.allow_permitted(pending)):
            raise p.RelayError(p.ErrorCode.INPUT_CONFLICT)
        if len(self.applications) >= 128:
            raise p.RelayError(p.ErrorCode.LIMIT)
        observer = pending.get('application' if effect == 'deny' else 'allow_application')
        self.applications[identifier] = {'digest': digest, 'effect': effect, 'observer': observer, 'reply_written': False,
                                         'reply_done': asyncio.Event()}
        if deny_reference is not None:
            if effect != 'deny' or not p.valid_digest(deny_reference):
                raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            pending['denyReference'] = deny_reference
        pending['claimed'] = True
        pending['verify_answer'] = verify
        pending['future'].set_result('allow' if effect == 'allow_once' else 'deny')
        try:
            await asyncio.wait_for(self.applications[identifier]['reply_done'].wait(), 3)
        except TimeoutError:
            return {'outcome': 'unknown'}
        return await self.inspect_application(identifier, digest, effect, timeout=3)

    async def inspect_application(self, identifier, digest, effect, timeout=.01):
        record = self.applications.get(identifier)
        if not record or record['digest'] != digest or record['effect'] != effect:
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        if record.get('result') is not None:
            return record['result']
        observer = record['observer']
        if observer is not None:
            # Read-only bounded observation after the one reply. Never retry it.
            deadline = asyncio.get_running_loop().time() + timeout
            while asyncio.get_running_loop().time() < deadline:
                try:
                    if not record['reply_written']:
                        await asyncio.sleep(.01)
                        continue
                    observed = await asyncio.to_thread(observer)
                    if observed is None:
                        raise ValueError('Native application evidence is not available')
                    application = observed if isinstance(observed, dict) else {'nativeRef': observed}
                    entry = self.tool_requests.get(application.get('nativeToolId'))
                    if entry and entry['decision'] == identifier:
                        self.tool_requests.pop(application['nativeToolId'])
                    result = {'outcome': 'accepted', 'application': {**application, 'decisionId': identifier,
                        'actionDigest': digest, 'effect': effect}}
                    record['result'] = result
                    return result
                except (p.RelayError, OSError, ValueError):
                    await asyncio.sleep(.05)
        return {'outcome': 'unknown'}  # a future/socket write isn't provider admission

    async def close(self):
        self.token = ''
        self.tool_bindings.active.clear()
        self.tool_bindings.ambiguous.clear()
        self.tool_bindings.allowed.clear()
        self.tool_bindings.completed.clear()
        if self.server:
            self.server.close()
        for pending in self.pending.values():
            if not pending['future'].done():
                pending['future'].set_result('deny')
        handlers = tuple(self.handlers)
        if handlers:
            # SessionEnd can outlive its CLI process while its fresh main
            # authority response is in flight. Cancelling that observation
            # cancels RuntimePeer.call and fences the replacement owner's
            # shared channel. Retire the token/listener first, then let bounded
            # observations/explicit denies drain. Truly stuck work still fences.
            _, pending = await asyncio.wait(handlers, timeout=5)
            for task in pending:
                task.cancel()
            await asyncio.gather(*handlers, return_exceptions=True)
        if self.server:
            await self.server.wait_closed()
        try:
            info = self.directory.stat(follow_symlinks=False)
            if (info.st_dev, info.st_ino) == self.identity:
                for name in ('native-hook.sock', 'owner-channel.json'):
                    (self.directory / name).unlink(missing_ok=True)
        finally:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
