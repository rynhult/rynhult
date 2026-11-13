"""L1 owner transition for one Claude conversation, called under hub/broker admission.

The terminal owner is the existing broker TUI owner with documented L2 hooks,
not an SDK sidecar. Its prepare_switch must positively establish empty/drained
input, no pending decisions/tools and idle; stop_owner must verify process death.
Launch callbacks keep the reviewed launch policy and return an owner plus attestation.
This coordinator does not launch on attach, read, reconnect or preference changes.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from typing import Awaitable, Callable

from ..protocol import ErrorCode, RelayError
from .base import CloseMode, ExitState, ReviewedSpec, RuntimeAttestation, SessionAdapter


@dataclass(frozen=True)
class LaunchedClaudeOwner:
    owner: SessionAdapter
    attestation: RuntimeAttestation


@dataclass(frozen=True)
class ClaudeSwitchResult:
    view: str
    run_id: str
    native_conversation: str = field(repr=False)
    changed: bool = True


class IdleOwnerSwitch:
    """One explicitly admitted tab selection; no queued or automatic retry.

    The caller holds the exclusive account/runtime conversation lease for the
    entire transition and fences new turn/terminal-input admission.

    `authorize` rechecks the hub's claimed switch plan, epochs and frozen policy,
    including draining/fencing terminal input. It is required both before stopping
    and before launching. A failed target launch leaves recovery required with no
    owner; it never automatically relaunches either side.
    """

    def __init__(self, *, spec: ReviewedSpec, view: str, owner: SessionAdapter,
                 launch_chat: Callable[[ReviewedSpec], Awaitable[LaunchedClaudeOwner]],
                 launch_terminal: Callable[[ReviewedSpec], Awaitable[LaunchedClaudeOwner]],
                 authorize: Callable[[ReviewedSpec, ReviewedSpec], Awaitable[bool]],
                 checkpoint=None, preflight=None):
        if (view not in ('chat', 'terminal') or spec.binding.provider != 'claude'
                or not spec.binding.native_conversation):
            raise RelayError(ErrorCode.INTEGRITY_FAILURE)
        self.spec, self.view, self.owner = spec, view, owner
        self.launch = {'chat': launch_chat, 'terminal': launch_terminal}
        self.authorize = authorize
        self.checkpoint, self.preflight = checkpoint, preflight
        self.lock = asyncio.Lock()
        self.recovery_required = False
        self.inline_reason = None

    def _validate(self, target):
        # ID equality alone is insufficient across accounts/machines/workspaces.
        if (target.binding != self.spec.binding or target.run_id == self.spec.run_id or
                target.spec.get('session_id') != self.spec.spec.get('session_id') or
                target.spec.get('settings_path') != self.spec.spec.get('settings_path')):
            raise RelayError(ErrorCode.STALE_RUN)
        def frozen(value):
            return {key: item for key, item in value.items() if key != 'prior_owner_exit_verified'}
        if frozen(target.spec) != frozen(self.spec.spec):
            raise RelayError(ErrorCode.POLICY_REFUSED)
        if target.spec.get('checkpoint') or target.spec.get('fork_session'):
            raise RelayError(ErrorCode.INTEGRITY_FAILURE)

    async def select(self, view: str, target: ReviewedSpec) -> ClaudeSwitchResult:
        if view not in self.launch:
            raise RelayError(ErrorCode.UNSUPPORTED)
        if self.lock.locked():
            self.inline_reason = 'A session switch is already in progress.'
            raise RelayError(ErrorCode.BUSY)
        async with self.lock:
            if self.recovery_required or self.owner is None:
                self.inline_reason = 'The previous switch needs recovery before switching again.'
                raise RelayError(ErrorCode.INTEGRITY_FAILURE)
            if view == self.view:
                return ClaudeSwitchResult(view, self.spec.run_id, self.spec.binding.native_conversation, False)
            self.inline_reason = None
            self._validate(target)
            if not await self.authorize(self.spec, target):
                raise RelayError(ErrorCode.POLICY_REFUSED)
            evidence = await self.owner.prepare_switch()
            if not evidence.idle:
                self.inline_reason = f'Finish current work and pending decisions before switching to {view.title()}.'
                raise RelayError(ErrorCode.BUSY)  # rendered inline; no pending transition
            if (evidence.native_conversation != self.spec.binding.native_conversation or
                    evidence.exit is ExitState.UNKNOWN or
                    evidence.policy_digest != self.spec.binding.launch_policy_digest):
                raise RelayError(ErrorCode.INTEGRITY_FAILURE)
            if self.preflight:
                await self.preflight(view, target)
            if not await self.authorize(self.spec, target):
                raise RelayError(ErrorCode.POLICY_REFUSED)
            if self.checkpoint:
                await self.checkpoint('stopping', evidence)
            # Stop may have succeeded even when its response is lost. From here,
            # every exception requires explicit recovery, never a blind retry.
            self.recovery_required = True
            self.inline_reason = 'The switch did not finish; review session recovery.'
            stopped = await self.owner.stop_owner()
            if (not stopped.idle or stopped.exit is not ExitState.EXITED or
                    stopped.native_conversation != self.spec.binding.native_conversation or
                    stopped.policy_digest != self.spec.binding.launch_policy_digest):
                raise RelayError(ErrorCode.INTEGRITY_FAILURE)
            self.owner = None
            if self.checkpoint:
                await self.checkpoint('starting', stopped)
            self.inline_reason = 'Session stopped — switch did not finish.'
            if not await self.authorize(self.spec, target):
                raise RelayError(ErrorCode.POLICY_REFUSED)
            reviewed = replace(target, spec={**target.spec, 'prior_owner_exit_verified': True})
            # No overlap: launch is unreachable before positive old-owner exit.
            launched = await self.launch[view](reviewed)
            self.owner = launched.owner  # retain it for explicit recovery even if identity fails
            observed = launched.attestation
            if (observed.binding_id != reviewed.binding.binding_id or observed.run_id != reviewed.run_id or
                    observed.native_conversation != reviewed.binding.native_conversation or not observed.process_identity):
                await self.owner.close(CloseMode.STOP)
                raise RelayError(ErrorCode.INTEGRITY_FAILURE)
            if not await self.authorize(self.spec, target):
                await self.owner.close(CloseMode.STOP)
                raise RelayError(ErrorCode.POLICY_REFUSED)
            if self.checkpoint:
                await self.checkpoint('owned', observed)
            self.spec, self.view = reviewed, view
            self.recovery_required = False
            self.inline_reason = None
            return ClaudeSwitchResult(view, reviewed.run_id, reviewed.binding.native_conversation)


# Compatibility name for callers/tests of the Claude-specific seam. The generic
# process coordinator has no NativeAction/ControlBinding/proxy dependency.
# Codex writer-only handover uses writer_lease, retaining its execution owner.
ClaudeL1Switch = IdleOwnerSwitch


def terminal_resume_command(spec: ReviewedSpec) -> tuple[str, ...]:
    """Existing broker launch uses these required flags alongside its frozen policy.

    Never invoke this from a read/tab render. The broker also supplies the launch's
    plugins/MCP/instructions and original hook environment, as with native resume.
    """
    if (spec.binding.provider != 'claude' or not spec.binding.native_conversation or
            spec.spec.get('prior_owner_exit_verified') is not True or
            spec.spec.get('guard_intact') is not True or not spec.spec.get('settings_path')):
        raise RelayError(ErrorCode.INTEGRITY_FAILURE)
    return (spec.spec['cli_path'], '--resume', spec.binding.native_conversation,
            '--settings', spec.spec['settings_path'])
