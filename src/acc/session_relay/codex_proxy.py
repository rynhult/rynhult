"""Deny-by-default native request mediation; never an app-server RPC tunnel.

The pinned stock TUI has no qualified input-watermark correlation. Production
therefore cannot register native messages. An isolated harness can supply proven
correlations and exercise hub admission without fabricating Terminal parity.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass

from .adapters.base import AdmissionResult, NativeClientProxy
from .protocol import ControlBinding, ErrorCode, NativeAction, RelayError, attribute_native_action


@dataclass(frozen=True)
class CorrelatedRequest:
    action: NativeAction
    method: str
    params: dict


class CodexNativeProxy(NativeClientProxy):
    def __init__(self, bindings, admit_to_hub, *, correlation_qualified=False, capacity=128):
        self.bindings, self.admit_to_hub = bindings, admit_to_hub
        self.correlation_qualified, self.capacity = correlation_qualified, capacity
        self.pending: dict[str, CorrelatedRequest] = {}
        self.closed = False

    def register_correlated(self, request: CorrelatedRequest):
        if self.closed or not self.correlation_qualified:
            raise RelayError(ErrorCode.UNSUPPORTED)
        # Basic text and exact active-turn cancellation only. Configuration,
        # approvals, thread loading/creation, attachments, steer and raw replies
        # cannot bypass missing hub controls through the native client.
        allowed = {'turn/start': {'text'}, 'turn/interrupt': {'turnId'}}
        fields = allowed.get(request.method)
        if fields is None or request.params.keys() != fields:
            raise RelayError(ErrorCode.UNSUPPORTED)
        if not all(type(value) is str and value for value in request.params.values()):
            raise RelayError(ErrorCode.INTEGRITY_FAILURE)
        if request.action.native_ref in self.pending:
            raise RelayError(ErrorCode.INTEGRITY_FAILURE)
        if len(self.pending) >= self.capacity:
            raise RelayError(ErrorCode.LIMIT)
        self.pending[request.action.native_ref] = copy.deepcopy(request)

    async def admit(self, action: NativeAction) -> AdmissionResult:
        if self.closed or not self.correlation_qualified:
            raise RelayError(ErrorCode.UNSUPPORTED)
        request = self.pending.pop(action.native_ref, None)
        if request is None or request.action != action:
            raise RelayError(ErrorCode.INPUT_CONFLICT, details={'reason': 'unattributed'})
        binding: ControlBinding = attribute_native_action(action, self.bindings())
        # The hub derives current permissions and registers a durable operation.
        # Never forward the native message or use today's controller as the actor.
        return await self.admit_to_hub(binding, request.method, request.params)

    async def close(self):
        self.closed = True
        self.pending.clear()
