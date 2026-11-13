"""Broker-owned Claude TUI observations for L1. Hooks alone never prove idle.

The broker must supply its owned process, authenticated native hooks and a
qualified TerminalHost draft/drain boundary. This does not launch on attach and
cannot infer idle from a quiet PTY or a Stop hook alone. Production enrollment
remains unavailable until those concrete observations are qualified.
"""
from __future__ import annotations

import asyncio

from ..protocol import ErrorCode, RelayError
from .base import CloseMode, ExitState, SwitchEvidence, UnavailableAdapter


class ClaudeTerminalOwner(UnavailableAdapter):
    def __init__(self, *, spec, process, host, grant, boundary, authorize, tree_exited):
        self.spec, self.process, self.host, self._grant = spec, process, host, grant
        self.boundary, self.authorize, self.tree_exited = boundary, authorize, tree_exited
        self.idle = False  # only a trusted Stop observation may change this
        self.tools = set()
        self.background = set()
        self.decisions = set()
        self.failed = False
        self.revision = 0
        self.lock = asyncio.Lock()

    @property
    def grant(self):
        return getattr(self.host, 'grant', None) or self._grant

    def observe_hook(self, payload):
        """Called only by the broker's authenticated run-bound hook receiver.

        No HTTP/native client may call this directly. Session identity must match
        even on that private receiver. Unknown/malformed events poison idle proof.
        Tool inputs and prompts aren't retained in the ownership ledger.
        """
        if self.failed:
            raise RelayError(ErrorCode.INTEGRITY_FAILURE)
        if payload.get('session_id') != self.spec.binding.native_conversation:
            self.failed = True
            raise RelayError(ErrorCode.STALE_RUN)
        self.revision += 1
        event = payload.get('hook_event_name')
        identifier = payload.get('tool_use_id') or payload.get('_relay_decision_id')
        if event in ('PostToolUse', 'PostToolUseFailure'):
            from ..claude_hook import background_task_id
            try:
                background = background_task_id(payload)
            except ValueError:
                self.failed = True
                raise RelayError(ErrorCode.INTEGRITY_FAILURE) from None
            if background:
                key = ('native-result', background)  # No qualified native completion hook.
                if key not in self.background and len(self.background) >= 64:
                    self.failed = True
                    raise RelayError(ErrorCode.LIMIT)
                self.background.add(key)
        if event == 'UserPromptSubmit':
            self.idle = False
        elif event in ('SessionStart', 'SessionEnd', 'Notification'):
            pass  # Observations only; neither establishes idle.
        elif event == 'Stop':
            self.idle = True
        elif event == 'PreToolUse' and isinstance(identifier, str) and identifier:
            self.tools.add(identifier)
            self.idle = False
        elif event in ('PostToolUse', 'PostToolUseFailure') and identifier in self.tools:
            self.tools.remove(identifier)
        elif event == 'PermissionRequest' and isinstance(identifier, str) and identifier:
            self.decisions.add(identifier)
            self.idle = False
        elif event == 'PermissionDenied' and identifier in self.decisions:
            self.decisions.remove(identifier)
        elif event == 'TaskCompleted' and payload.get('task_id') in self.background:
            self.background.remove(payload['task_id'])
        elif event == 'TaskStarted' and isinstance(payload.get('task_id'), str):
            self.background.add(payload['task_id'])
            self.idle = False
        else:
            # SessionStart/Notification/unknown vendor events cannot establish
            # absence of active/background work. Qualification must map them.
            self.failed = True
        if max(len(self.tools), len(self.background), len(self.decisions)) > 64:
            self.failed = True
            raise RelayError(ErrorCode.LIMIT)

    def confirm_denial(self, identifier):
        # Internal broker-owned completed-chain evidence only. A denied native
        # tool has no PostToolUse hook, so leaving PreToolUse here invents busy.
        if not isinstance(identifier, str) or not identifier:
            raise RelayError(ErrorCode.INTEGRITY_FAILURE)
        self.tools.discard(identifier)
        self.revision += 1

    def permission_closed(self, identifier):
        # Broker closes exactly its hook invocation. This is not tool completion
        # or proof of idle; Stop and background/tool ledgers remain required.
        self.decisions.discard(identifier)
        self.revision += 1

    async def prepare_switch(self):
        async with self.lock:
            return await self._prepare()

    async def _prepare(self):
        if not await self.authorize(self.spec):
            raise RelayError(ErrorCode.POLICY_REFUSED)
        revision = self.revision
        # Freeze new input. A nonempty old client may be retained read-only;
        # its unsent text never becomes processed or enters the new writer.
        if not self.idle or self.failed or self.tools or self.background or self.decisions:
            return SwitchEvidence(False, ExitState.RUNNING if self.process.returncode is None else ExitState.UNKNOWN,
                                  self.spec.binding.native_conversation, self.spec.binding.launch_policy_digest)
        boundary = await self.boundary(self.grant)
        if not await self.authorize(self.spec):
            raise RelayError(ErrorCode.POLICY_REFUSED)
        drained = await self.host.drain(self.grant, boundary.admitted)
        if not await self.authorize(self.spec):
            raise RelayError(ErrorCode.POLICY_REFUSED)
        retained=getattr(self.host,'retained_client',None)
        retained_valid=(retained is not None and retained.read_only
            and retained.epoch==boundary.epoch and retained.admitted==boundary.admitted
            and retained.processed==boundary.processed and retained.run_id==self.spec.run_id
            and retained.native_id==self.spec.binding.native_conversation)
        valid = (boundary.epoch == self.grant.epoch == drained.epoch
                 and boundary.admitted == drained.admitted and boundary.processed == drained.processed
                 and (boundary.admitted == boundary.processed or retained_valid))
        idle = (valid and revision == self.revision and self.idle and not self.failed
                and not self.tools and not self.background and not self.decisions)
        exit_state = ExitState.RUNNING
        if self.process.returncode is not None:
            exit_state = ExitState.EXITED if await self.tree_exited(self.process) else ExitState.UNKNOWN
            if not await self.authorize(self.spec):
                raise RelayError(ErrorCode.POLICY_REFUSED)
        idle = idle and revision == self.revision
        return SwitchEvidence(idle, exit_state, self.spec.binding.native_conversation,
                              self.spec.binding.launch_policy_digest,retained.proof() if retained_valid else None)

    async def stop_owner(self):
        async with self.lock:
            evidence = await self._prepare()
            if not evidence.idle:
                raise RelayError(ErrorCode.BUSY)
            revision = self.revision
            if not await self.authorize(self.spec):
                raise RelayError(ErrorCode.POLICY_REFUSED)
            if revision != self.revision:
                raise RelayError(ErrorCode.BUSY)
            if self.process.returncode is None:
                self.process.terminate()
                await asyncio.wait_for(self.process.wait(), 5)
            exited = await self.tree_exited(self.process)
            callback = getattr(self.host, 'close', None)
            if exited and callback:
                await callback()
            clean = self.idle and not self.failed and not self.tools and not self.background and not self.decisions
            return SwitchEvidence(clean, ExitState.EXITED if exited else ExitState.UNKNOWN,
                                  self.spec.binding.native_conversation, self.spec.binding.launch_policy_digest,
                                  evidence.retained_input)

    async def close(self, mode):
        if mode is CloseMode.STOP:
            # Explicit authorized Stop needn't be idle. Still reap the owner;
            # switching additionally needs the tree-exit proof above.
            if self.process.returncode is None:
                self.process.terminate()
                await asyncio.wait_for(self.process.wait(), 5)
        callback = getattr(self.host, 'close', None)
        if mode is CloseMode.STOP and callback:
            await callback()
