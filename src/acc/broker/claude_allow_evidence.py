"""Chosen TUI Allow: unique hook join and protected native tool application."""
import asyncio
import json
import os
from pathlib import Path

from acc.session_relay import protocol as p

from .claude_admission import MAX_RECORDS, MAX_TAIL, _locked
from .claude_denial_evidence import NativeDenial
from .claude_tool_binding import Completion, prompt_digest

CLAUDE_TUI_ALLOW_ACK_QUALIFIED = False


class NativeAllow(NativeDenial):
    @_locked
    def bind(self, pending):
        binding = {'toolId': pending['boundToolId'], 'promptId': pending['promptId'],
            'toolName': pending['toolName'], 'promptDigest': pending['promptDigest']}
        if (not all(p.valid_opaque(binding[key]) for key in ('toolId','promptId','toolName'))
                or not p.valid_digest(binding['promptDigest'])):
            raise p.RelayError(p.ErrorCode.INPUT_CONFLICT)
        return binding

    def source(self, binding, nodes):
        identifier, prompt = binding['toolId'], binding['promptId']
        candidates = []
        current_prompt = None
        prompt_rows = [row for row in nodes.values() if row.get('type') == 'user'
            and isinstance(row.get('turnPosition'), dict) and row.get('promptId') == prompt]
        if len(prompt_rows) != 1:
            return None
        for source, row in nodes.items():
            message = row.get('message', {})
            position = row.get('turnPosition', {})
            if (row.get('type') == 'user' and isinstance(message, dict) and message.get('role') == 'user'
                    and p.valid_opaque(row.get('promptId')) and isinstance(position, dict)
                    and all(type(position.get(key)) is int and position[key] >= 0 for key in ('promptIndex','turnIndex'))):
                current_prompt = (row['promptId'], [position['promptIndex'],position['turnIndex']])
            message = row.get('message', {})
            if row.get('type') != 'assistant' or not isinstance(message, dict) or message.get('role') != 'assistant':
                continue
            content = message.get('content')
            if not isinstance(content, list):
                continue
            for item in content:
                if (not isinstance(item, dict) or item.get('type') != 'tool_use' or item.get('id') != identifier
                        or item.get('name') != binding['toolName']):
                    continue
                key = prompt_digest({'session_id': self.native_id, 'prompt_id': prompt,
                    'tool_name': item['name'], 'tool_input': item.get('input')})
                if key != binding['promptDigest']:
                    continue
                # The native transcript omits some successful file-tool result
                # parents. Its explicit prompt ID/sequence still partitions the
                # append order. Do not infer a tool identity from that order.
                if current_prompt is not None and current_prompt[0] == prompt:
                    sequence = current_prompt[1]
                    candidates.append({'toolId': identifier, 'promptId': prompt, 'source': source,
                        'promptSequence': sequence, 'toolName': item['name'],
                        'promptDigest': key, 'transcriptDigest': p.sha256_digest(p.canonical_json(
                            [self.native_id, prompt, sequence, item['name'], item['input']]))})
        if len(candidates) != 1:
            return None
        return candidates[0]

    @_locked
    def applied(self, binding, *, completion=None):
        if binding.keys() != {'toolId','promptId','toolName','promptDigest'}:
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        size = self._validate()
        if size < self.offset or size - self.offset > MAX_TAIL or self._hash(self.offset) != self.prefix:
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        raw = os.pread(self.fd, size - self.offset, self.offset)
        lines = raw[:raw.rfind(b'\n') + 1].splitlines()
        if len(lines) > MAX_RECORDS:
            raise p.RelayError(p.ErrorCode.LIMIT)
        nodes = dict(self.nodes)
        rows = []
        for line in lines:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            if row.get('uuid') is None:
                continue
            identifier, row = self.node(row)
            if (identifier in nodes or row.get('sessionId') != self.native_id or row.get('isSidechain') is not False):
                raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            nodes[identifier] = row
            rows.append(row)
        complete = self.source(binding, nodes)
        if complete is None:
            return None
        if completion is not None:
            if (not isinstance(completion, Completion) or completion.session != self.native_id
                    or completion.tool != binding['toolId'] or completion.digest != binding['promptDigest']
                    or completion.event not in ('PostToolUse', 'PostToolUseFailure')
                    or not p.valid_digest(completion.reference)):
                raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            self._validate()
            if self._hash(self.offset) != self.prefix or os.pread(self.fd, len(raw), self.offset) != raw:
                raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            reference = p.sha256_digest(p.canonical_json([self.native_id, complete, completion.reference]))
            if reference in self.used:
                return None
            self.used.add(reference)
            self.evidence_binding = complete
            return reference
        decisions, results = [], []
        for row in rows:
            identifier = row['uuid']
            attachment = row.get('attachment', {})
            if (row.get('type') == 'attachment' and isinstance(attachment, dict)
                    and attachment.get('type') == 'hook_permission_decision'
                    and attachment.get('hookEvent') == 'PermissionRequest'
                    and attachment.get('decision') == 'allow' and attachment.get('toolUseID') == binding['toolId']
                    and row.get('parentUuid') in nodes):
                decisions.append(identifier)
            permission = row.get('permissionDecision', {})
            if (row.get('type') != 'user' or row.get('promptId') != binding['promptId']
                    or row.get('sourceToolAssistantUUID') != complete['source']
                    or not isinstance(permission, dict) or permission.get('decision') != 'accept'
                    or permission.get('source') != 'hook' or permission.get('reasonType') != 'hook'):
                continue
            result = row.get('toolUseResult')
            if isinstance(result, dict) and result.get('interrupted') is True:
                continue
            message = row.get('message', {})
            content = message.get('content') if isinstance(message, dict) else None
            if (not isinstance(message, dict) or message.get('role') != 'user'
                    or not isinstance(content, list) or len(content) != 1):
                continue
            item = content[0]
            if (isinstance(item, dict) and item.get('type') == 'tool_result'
                    and item.get('tool_use_id') == binding['toolId'] and type(item.get('is_error')) is bool):
                results.append(identifier)
        if len(decisions) != 1 or len(results) != 1:
            return None
        self._validate()
        if self._hash(self.offset) != self.prefix or os.pread(self.fd, len(raw), self.offset) != raw:
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        reference = p.sha256_digest(p.canonical_json([self.native_id, complete, decisions, results]))
        if reference in self.used:
            return None
        self.used.add(reference)
        self.evidence_binding = complete
        return reference


async def observe(runtime, reader, binding, value, run_id, hooks):
    deadline = asyncio.get_running_loop().time() + 30
    reference = None
    try:
        while asyncio.get_running_loop().time() < deadline:
            try:
                reference = reference or reader.applied(binding, completion=hooks.tool_bindings.evidence(binding))
                runtime.guard_check()
            except (OSError, ValueError, p.RelayError):
                return {'outcome': 'unknown'}
            if reference:
                journal = getattr(runtime, 'decision_evidence', None)
                if journal is not None:
                    journal.verified(value['operation']['operationId'], reference)
                event = {'kind': 'decision.applied', 'decisionId': value['decisionId'],
                    'operationId': value['operation']['operationId'], 'choiceId': 'accept',
                    'actionDigest': value['actionDigest'], 'nativeRef': reference}
                try:
                    applied = await runtime.peer.call('publication', {**event, 'runId': run_id})
                    if applied is True and journal is not None:
                        journal.settled(value['operation']['operationId'])
                    return {'outcome': 'accepted' if applied is True else 'unknown'}
                except (ConnectionError, TimeoutError):
                    if runtime.peer.closed:
                        return {'outcome': 'unknown'}
            await asyncio.sleep(.05)
        return {'outcome': 'unknown'}
    finally:
        hooks.tool_bindings.retire(binding)
        reader.close()


async def answer(runtime, value, verify):
    hooks = runtime.owners.hooks
    pending = hooks.pending[value['decisionId']]
    if not hooks.tool_bindings.permits(pending, hooks.pending):
        raise p.RelayError(p.ErrorCode.INPUT_CONFLICT)
    source = runtime.native_transcript
    if not source or source[0] != runtime.spec.run_id:
        raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
    env = runtime.owners.environment(runtime.spec)
    root = Path(env.get('CLAUDE_CONFIG_DIR', str(Path(env['HOME']) / '.claude'))) / 'projects'
    reader = NativeAllow(root, source[1], runtime.spec.binding.native_conversation)
    transferred = False
    try:
        reader.begin()
        binding = reader.bind(pending)
        from .claude_decision_journal import DecisionEvidenceJournal, checkpoint
        journal = getattr(runtime, 'decision_evidence', None)
        if journal is None:
            journal = runtime.decision_evidence = DecisionEvidenceJournal(runtime.owners.directory / 'decision-evidence')
        hooks.evidence_journal = journal
        journal.prepare(vars(runtime.spec.binding), {
            'event': {'kind': 'decision.applied', 'decisionId': value['decisionId'],
                'operationId': value['operation']['operationId'], 'choiceId': 'accept', 'actionDigest': value['actionDigest']},
            'runId': str(runtime.spec.run_id), 'reader': checkpoint(reader), 'binding': binding,
            'operation': {key: value['operation'][key] for key in ('operationId','runId','method','actor','payloadDigest')}})
        tasks = getattr(runtime, 'denial_observers', None)
        if tasks is None:
            tasks = runtime.denial_observers = set()
        if getattr(runtime, 'denial_observers_closed', False) or len(tasks) >= 16:
            raise p.RelayError(p.ErrorCode.LIMIT)
        async def verified():
            if not await verify():
                return False
            reader._validate()
            if reader._hash(reader.offset) != reader.prefix:
                raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            return True
        result = await hooks.answer(value['decisionId'], value['actionDigest'], value['effect'], verified)
        task = asyncio.create_task(observe(runtime, reader, binding, value, str(runtime.spec.run_id), hooks))
        tasks.add(task)
        transferred = True
        def finished(task):
            tasks.discard(task)
            if not task.cancelled():
                task.exception()
        task.add_done_callback(finished)
        done, _ = await asyncio.wait({task}, timeout=2)
        return task.result() if done else result
    finally:
        if not transferred:
            reader.close()
