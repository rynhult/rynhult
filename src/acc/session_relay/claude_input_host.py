"""Owned Claude framed input plus exact native admission and receipt-gated drain."""
import asyncio

from . import protocol as p
from .adapters.base import InputGrant
from .claude_draft import ClaudeDraft
from .terminal_host import TmuxTerminalHost


class ClaudeInputHost(TmuxTerminalHost):
    def __init__(self, run, opener, validate, *, observer, native_id, reader, register, settle, processed, authorize_boundary):
        self.writing_frame = self.native_prompt_active = False
        async def validate_write(grant):
            allowed = await validate(grant)
            return allowed and not (self.writing_frame and self.native_prompt_active)
        async def no_geometry(*args):
            return False
        super().__init__(run, opener, validate_write, no_geometry, consume_output=True)
        self.observer, self.native_id, self.admission = observer, native_id, reader
        self.register, self.settle, self.on_processed = register, settle, processed
        self.authorize_boundary = authorize_boundary
        self.closed = False
        self.close_task = None
        self.ending = False
        self.boundary = self.evidence = None
        self.input_lock = asyncio.Lock()
        self.attested = False

    async def attach_read(self, run):
        async for data in self.observer.attach_read(run):
            yield data

    async def claim_input(self, grant):
        self._run(grant.run)
        if not isinstance(grant, InputGrant):
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        async with self.input_lock:
            if self.closed or (self.boundary is not None and self.boundary.grant != grant):
                raise p.RelayError(p.ErrorCode.INPUT_CONFLICT)
            await super().claim_input(grant)
            if self.boundary is None:
                self.boundary = ClaudeDraft(grant, self.native_id)
            elif self.boundary.grant != grant:
                raise p.RelayError(p.ErrorCode.INPUT_CONFLICT)

    async def write_frames(self, grant, sequence, data):
        async with self.input_lock:
            if self.native_prompt_active:
                raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
            if self.closed or self.ending or self.boundary is None or not await self.validate_input(grant):
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            if self.admitted == self.processed and self.boundary.ticket is None:
                await asyncio.to_thread(self.admission.begin)
                if not await self.validate_input(grant):
                    raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            self.boundary.reserve(grant, sequence, data)
            try:
                self.writing_frame = True
                result = await super().write_frames(grant, sequence, data)
                if result.partial:
                    self.boundary.poison()
                return result
            except BaseException:
                self.boundary.poison()
                raise
            finally:
                self.writing_frame = False

    async def observe_native(self, payload):
        event = payload.get('hook_event_name')
        if event == 'SessionEnd':
            self.ending = True  # end is not an idle/draft proof; later input refuses
            return
        if event not in ('UserPromptSubmit', 'Stop'):
            return
        async with self.input_lock:
            boundary = self.boundary
            if boundary is None:
                if event == 'UserPromptSubmit':
                    raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
                return
            try:
                if event == 'UserPromptSubmit':
                    if not await self.validate_input(self.grant):
                        raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
                    prompt = payload.get('prompt')
                    if not isinstance(prompt, str):
                        raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
                    boundary.observe_prompt(payload.get('session_id'), p.sha256_digest(prompt.encode()))
                    operation = await self.register(self.grant, boundary.ticket[0], boundary.ticket[1])
                    boundary.bind_operation(operation)
                    if not await self.validate_input(self.grant):
                        raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
                elif boundary.operation is not None:
                    if payload.get('session_id') != self.native_id:
                        raise p.RelayError(p.ErrorCode.STALE_RUN)
                    # The completed native provider chain is evidence, not Stop.
                    read = getattr(self.admission, 'wait_evidence', self.admission.evidence)
                    self.evidence = await asyncio.to_thread(read, boundary.operation)
                    receipt = await self.settle(self.evidence)
                    if self.closed or self.ending or not await self.validate_input(self.grant):
                        raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
                    sequence, operation_id = boundary.ticket[0], boundary.operation.operation_id
                    boundary.confirm(receipt)
                    self.processed = boundary.processed
                    await self.on_processed(self.grant, sequence, operation_id)
                    self.evidence = None
            except BaseException:
                boundary.poison()
                raise

    async def freeze_boundary(self, grant):
        async with self.input_lock:
            if (self.closed or self.boundary is None or self.grant != grant
                    or self.admitted != self.boundary.admitted or self.processed != self.boundary.processed
                    or not await self.authorize_boundary()):
                raise p.RelayError(p.ErrorCode.INPUT_CONFLICT)
            return self.boundary.freeze_for_handover(grant)

    @property
    def retained_client(self):
        return self.boundary.retained_client if self.boundary else None

    async def drain(self, grant, watermark):
        async with self.input_lock:
            if (self.closed or self.boundary is None or not self.boundary.frozen or self.grant != grant
                    or watermark != self.admitted or not await self.authorize_boundary()):
                raise p.RelayError(p.ErrorCode.INPUT_CONFLICT)
            return self.boundary.freeze_for_handover(grant)

    async def close(self):
        if self.close_task is None:
            self.closed = True
            self.close_task = asyncio.create_task(self._close())
        await asyncio.shield(self.close_task)

    async def _close(self):
        async with self.input_lock:
            try:
                if self.boundary is not None and self.boundary.operation is not None:
                    # Explicit Stop/revocation may prevent its Stop hook. Persist
                    # genuine late evidence, never a synthetic rejection/retry.
                    evidence = self.evidence or await asyncio.to_thread(self.admission.evidence, self.boundary.operation)
                    await self.settle(evidence)
            except (p.RelayError, OSError, ValueError, ConnectionError):
                pass
            finally:
                await self.detach(self.run)
                await asyncio.to_thread(self.admission.close)
