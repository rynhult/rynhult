"""Project stock TUI text against frozen native start settings.

This validates a wire envelope, never identifies its actor or grants input.
A separately owned immutable writer route and Hub admission remain mandatory.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from .protocol import MAX_PROMPT_BYTES, ErrorCode, RelayError, valid_opaque

# These optional stock fields must stay absent/null. In particular no steering,
# tool response, rich context, permission profile or per-turn config is tunneled.
NULL_FIELDS = frozenset({
    'parentTurnId', 'rootTurnId', 'toolOutput', 'responsesapiClientMetadata',
    'responsesapiClientMetadataPatch', 'additionalContext', 'environments',
    'sandboxPolicy', 'permissions', 'serviceTierForTurn', 'summary', 'personality',
    'outputSchema', 'collaborationMode', 'multiAgentMode', 'cyberAccessProgram',
})
FROZEN_FIELDS = frozenset({'cwd', 'runtimeWorkspaceRoots', 'approvalPolicy', 'approvalsReviewer', 'model', 'disabledPluginIds'})
FIELDS = NULL_FIELDS | FROZEN_FIELDS | {'threadId', 'input', 'clientUserMessageId', 'turnTrigger', 'serviceTier', 'effort'}


def basic_text(params):
    items = params.get('input')
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        raise RelayError(ErrorCode.UNSUPPORTED)
    item = items[0]
    if (item.keys() - {'type', 'text', 'text_elements'} or item.get('type') != 'text'
            or item.get('text_elements', []) != []):
        raise RelayError(ErrorCode.UNSUPPORTED)
    text = item.get('text')
    if not isinstance(text, str) or not text or len(text.encode()) > MAX_PROMPT_BYTES:
        raise RelayError(ErrorCode.UNSUPPORTED)
    return {'text': text}


@dataclass(frozen=True)
class NativeTurnPolicy:
    native_id: str
    # Keep an immutable copy. Mutating a caller's start response cannot widen it.
    settings_json: str

    @classmethod
    def from_start(cls, native_id, start):
        if not valid_opaque(native_id) or start.get('thread', {}).get('id') != native_id:
            raise RelayError(ErrorCode.INTEGRITY_FAILURE)
        return cls(native_id, json.dumps({key: start.get(key) for key in
            FROZEN_FIELDS | {'serviceTier', 'reasoningEffort'}}, sort_keys=True))

    def project(self, params):
        if not isinstance(params, dict) or params.keys() - FIELDS:
            raise RelayError(ErrorCode.UNSUPPORTED)
        if params.get('threadId') != self.native_id:
            raise RelayError(ErrorCode.STALE_RUN)
        if any(params.get(key) is not None for key in NULL_FIELDS):
            raise RelayError(ErrorCode.UNSUPPORTED)
        settings = json.loads(self.settings_json)
        for key in FROZEN_FIELDS:
            # Null/absent means retain the private owner's existing setting.
            if params.get(key) is not None and json.dumps(params[key], sort_keys=True) != json.dumps(settings[key], sort_keys=True):
                raise RelayError(ErrorCode.POLICY_REFUSED)
        if params.get('effort') is not None and params['effort'] != settings['reasoningEffort']:
            raise RelayError(ErrorCode.POLICY_REFUSED)
        tier = settings['serviceTier']
        if params.get('serviceTier') not in (None, tier if tier is not None else 'default'):
            raise RelayError(ErrorCode.POLICY_REFUSED)
        if params.get('turnTrigger') not in (None, 'user'):
            raise RelayError(ErrorCode.UNSUPPORTED)
        if params.get('clientUserMessageId') is not None and not valid_opaque(params['clientUserMessageId']):
            raise RelayError(ErrorCode.UNSUPPORTED)
        # Client IDs are display metadata, never actor, operation, grant or lease.
        return basic_text(params)
