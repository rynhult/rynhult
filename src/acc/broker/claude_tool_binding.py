"""Run-local native hook identity; identical pending inputs never choose an ID."""
from dataclasses import dataclass

from acc.session_relay import protocol as p


@dataclass(frozen=True)
class Completion:
    session: str
    tool: str
    digest: str
    event: str
    reference: str


def prompt_digest(payload):
    session, prompt, tool = (payload.get(key) for key in ('session_id', 'prompt_id', 'tool_name'))
    if (not p.valid_opaque(session) or not p.valid_opaque(prompt)
            or not p.valid_opaque(tool) or not isinstance(payload.get('tool_input'), dict)):
        return None
    return p.sha256_digest(p.canonical_json([session, prompt, tool, payload['tool_input']]))


class ToolBindings:
    def __init__(self):
        self.active = {}
        self.ambiguous = set()
        self.allowed = {}
        self.completed = {}

    def allow_sent(self, pending):
        if len(self.allowed) >= 128:
            raise p.RelayError(p.ErrorCode.LIMIT)
        self.allowed[pending['boundToolId']] = pending['promptDigest']

    def evidence(self, binding):
        value = self.completed.get(binding['toolId'])
        return value if value and value.digest == binding['promptDigest'] else None

    def retire(self, binding):
        identifier, digest = binding['toolId'], binding['promptDigest']
        if self.allowed.get(identifier) == digest:
            self.allowed.pop(identifier, None)
        value = self.completed.get(identifier)
        if value and value.digest == digest:
            self.completed.pop(identifier, None)

    def observe(self, payload):
        event = payload.get('hook_event_name')
        identifier = payload.get('tool_use_id')
        if event == 'PreToolUse':
            digest = prompt_digest(payload)
            if digest is None or not p.valid_opaque(identifier):
                return
            if len(self.active) >= 64:
                raise p.RelayError(p.ErrorCode.LIMIT)
            if identifier in self.active or digest in self.active.values():
                if len(self.ambiguous) >= 64:
                    raise p.RelayError(p.ErrorCode.LIMIT)
                self.ambiguous.add(digest)
            self.active[identifier] = digest
        elif event in ('PostToolUse', 'PostToolUseFailure', 'PermissionDenied'):
            digest = prompt_digest(payload)
            if (event in ('PostToolUse', 'PostToolUseFailure') and digest is not None
                    and self.active.get(identifier) == digest and self.allowed.get(identifier) == digest
                    and payload.get('is_interrupt', False) is False):
                if len(self.completed) >= 128:
                    raise p.RelayError(p.ErrorCode.LIMIT)
                self.completed[identifier] = Completion(payload['session_id'], identifier, digest, event,
                    p.sha256_digest(p.canonical_json([payload['session_id'], digest, identifier, event])))
            self.active.pop(identifier, None)
        elif event == 'UserPromptSubmit' and not self.active:
            self.ambiguous.clear()

    def bind(self, payload):
        digest = prompt_digest(payload)
        if digest is None:
            return None
        supplied = payload.get('tool_use_id')
        if supplied is not None:
            return supplied if p.valid_opaque(supplied) and self.active.get(supplied) == digest else None
        if digest in self.ambiguous:
            return None
        candidates = [identifier for identifier, value in self.active.items() if value == digest]
        return candidates[0] if len(candidates) == 1 else None

    def permits(self, pending, all_pending):
        digest = pending.get('promptDigest')
        identifier = pending.get('boundToolId')
        if digest is None or not p.valid_opaque(identifier) or self.active.get(identifier) != digest:
            return False
        if len([value for value in all_pending.values() if value.get('boundToolId') == identifier]) != 1:
            return False
        if pending.get('nativeToolId') == identifier:
            return True
        return (digest not in self.ambiguous and
                len([value for value in all_pending.values() if value.get('promptDigest') == digest]) == 1)
