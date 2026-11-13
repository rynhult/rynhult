"""Exact native denial output for one chosen private hook invocation."""
import json
import os

from acc.session_relay import protocol as p
from acc.session_relay.claude_hook import denial_message

from .claude_admission import MAX_RECORDS, MAX_TAIL, NativeAdmission, _locked

CLAUDE_TUI_DENY_ACK_QUALIFIED = False


class NativeDenial(NativeAdmission):
    @staticmethod
    def node(row):
        identifier = row.get('uuid')
        parent = row.get('parentUuid')
        if not p.valid_opaque(identifier) or parent is not None and not p.valid_opaque(parent):
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        return identifier, row

    @_locked
    def begin(self):
        super().begin()
        self.nodes = {}
        lines = os.pread(self.fd, self.offset, 0).splitlines()
        if len(lines) > MAX_RECORDS:
            raise p.RelayError(p.ErrorCode.LIMIT)
        for line in lines:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            if row.get('uuid') is None:
                continue
            identifier, row = self.node(row)
            if (identifier in self.nodes or row.get('sessionId') != self.native_id
                    or row.get('isSidechain') is not False):
                raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            self.nodes[identifier] = row
        if self._hash(self.offset) != self.prefix:
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)

    @_locked
    def denied(self, reference, prompt_id):
        if self.offset is None or not p.valid_digest(reference) or not p.valid_opaque(prompt_id):
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        size = self._validate()
        if size < self.offset or size - self.offset > MAX_TAIL or self._hash(self.offset) != self.prefix:
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        raw = os.pread(self.fd, size - self.offset, self.offset)
        lines = raw[:raw.rfind(b'\n') + 1].splitlines()
        if len(lines) > MAX_RECORDS:
            raise p.RelayError(p.ErrorCode.LIMIT)
        decisions, results, seen = [], [], set()
        nodes = dict(self.nodes)
        for line in lines:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            identifier = row.get('uuid')
            if identifier is None:
                continue
            if (not p.valid_opaque(identifier) or identifier in seen
                    or row.get('sessionId') != self.native_id or row.get('isSidechain') is not False):
                raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            self.node(row)
            if identifier in nodes:
                raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            seen.add(identifier)
            nodes[identifier] = row
            attachment = row.get('attachment', {})
            if (row.get('type') == 'attachment' and isinstance(attachment, dict)
                    and attachment.get('type') == 'hook_permission_decision'
                    and attachment.get('hookEvent') == 'PermissionRequest'
                    and attachment.get('decision') == 'deny'):
                decisions.append((row.get('parentUuid'), attachment.get('toolUseID')))
            if (row.get('type') != 'user' or row.get('promptId') != prompt_id
                    or row.get('permissionDecision') != {'decision': 'reject', 'reasonCode': 'hook:PermissionRequest',
                        'reasonType': 'hook', 'source': 'hook'}
                    or row.get('toolDenialKind') != 'permission-rule'
                    or row.get('toolUseResult') != 'Error: ' + denial_message(reference)):
                continue
            message = row.get('message', {})
            content = message.get('content') if isinstance(message, dict) else None
            if not isinstance(message, dict) or message.get('role') != 'user' or not isinstance(content, list) or len(content) != 1:
                continue
            item = content[0]
            parent = row.get('parentUuid')
            if (not isinstance(item, dict) or item.get('type') != 'tool_result' or item.get('is_error') is not True
                    or item.get('content') != denial_message(reference)
                    or not p.valid_opaque(item.get('tool_use_id')) or not p.valid_opaque(parent)
                    or row.get('sourceToolAssistantUUID') != parent):
                continue
            results.append((parent, item['tool_use_id'], identifier))
        if len(results) != 1:
            return None
        parent, tool, identifier = results[0]
        source = nodes.get(parent, {})
        message = source.get('message', {})
        content = message.get('content') if isinstance(message, dict) else None
        if (not isinstance(message, dict) or source.get('type') != 'assistant' or message.get('role') != 'assistant'
                or not p.valid_opaque(message.get('id')) or not isinstance(content, list)):
            return None
        tools = [item for item in content if isinstance(item, dict) and item.get('type') == 'tool_use'
                 and item.get('id') == tool]
        # Native parallel attachments can follow a sibling result. Their parent
        # is not invocation identity: use the typed tool ID, source tool block,
        # exact query ID and the private reference in the typed native rejection.
        if (len(tools) != 1 or getattr(self, 'tool_name', tools[0].get('name')) != tools[0].get('name')
                or len([1 for ancestor, identifier in decisions if identifier == tool
                        and ancestor in nodes]) != 1):
            return None
        self._validate()
        if self._hash(self.offset) != self.prefix or os.pread(self.fd, len(raw), self.offset) != raw:
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        native_ref = p.sha256_digest(p.canonical_json([self.native_id, reference, parent, tool, identifier]))
        if native_ref in self.used:
            return None
        self.used.add(native_ref)
        return native_ref


async def _observe(runtime, reader, reference, prompt_id, value, run_id):
    """Read-only reconciliation. Only native evidence publication may be retried."""
    import asyncio
    deadline = asyncio.get_running_loop().time() + 30
    native_ref = None
    try:
        while asyncio.get_running_loop().time() < deadline:
            try:
                if native_ref is None:
                    native_ref = reader.denied(reference, prompt_id)
                runtime.guard_check()
            except (OSError, ValueError, p.RelayError):
                return {'outcome': 'unknown'}
            if native_ref:
                event = {'kind': 'decision.applied', 'decisionId': value['decisionId'],
                    'operationId': value['operation']['operationId'], 'choiceId': 'decline',
                    'actionDigest': value['actionDigest'], 'nativeRef': native_ref}
                try:
                    if str(runtime.spec.run_id) == run_id:
                        applied = await runtime.publish(event)
                    else:
                        applied = await runtime.peer.call('publication', {**event, 'runId': run_id})
                    if applied is True:
                        return {'outcome': 'accepted'}
                    return {'outcome': 'unknown'}
                except (ConnectionError, TimeoutError):
                    if runtime.peer.closed:
                        return {'outcome': 'unknown'}
                    # Same evidence and original operation; never hooks.answer.
            await asyncio.sleep(.05)
        return {'outcome': 'unknown'}
    finally:
        reader.close()


async def close(runtime):
    import asyncio
    runtime.denial_observers_closed = True
    tasks = tuple(getattr(runtime, 'denial_observers', ()))
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def answer(runtime, value, verify):
    """Deny only. Foreground timeout preserves read-only original reconciliation."""
    import asyncio
    import secrets
    from pathlib import Path

    hooks = runtime.owners.hooks
    identifier = value['decisionId']
    pending = hooks.pending[identifier]
    prompt_id = pending.get('promptId')
    source = runtime.native_transcript
    tasks = getattr(runtime, 'denial_observers', None)
    if tasks is None:
        tasks = runtime.denial_observers = set()
    if (getattr(runtime, 'denial_observers_closed', False) or len(tasks) >= 16
            or not source or source[0] != runtime.spec.run_id or not p.valid_opaque(prompt_id)):
        return await hooks.answer(identifier, value['actionDigest'], value['effect'], verify)
    env = runtime.owners.environment(runtime.spec)
    root = Path(env.get('CLAUDE_CONFIG_DIR', str(Path(env['HOME']) / '.claude'))) / 'projects'
    reader = None
    try:
        reader = NativeDenial(root, source[1], runtime.spec.binding.native_conversation)
        if pending.get('toolName') is not None:
            reader.tool_name = pending['toolName']
        reader.begin()
    except (OSError, ValueError, p.RelayError):
        if reader:
            reader.close()
        return await hooks.answer(identifier, value['actionDigest'], value['effect'], verify)
    transferred = False
    try:
        run_id = str(runtime.spec.run_id)
        reference = p.sha256_digest(secrets.token_bytes(32))
        result = await hooks.answer(identifier, value['actionDigest'], value['effect'], verify, deny_reference=reference)
        task = asyncio.create_task(_observe(runtime, reader, reference, prompt_id, value, run_id))
        tasks.add(task)
        transferred = True
        def finished(task):
            tasks.discard(task)
            if not task.cancelled():
                task.exception()  # unexpected errors must not become orphaned task warnings
        task.add_done_callback(finished)
        done, _ = await asyncio.wait({task}, timeout=2)
        return task.result() if done else result
    finally:
        if not transferred:
            reader.close()
