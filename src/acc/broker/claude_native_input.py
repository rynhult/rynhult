"""Register/claim native input without dispatching it to another provider client."""
import json

from acc.session_relay import protocol as p
from acc.session_relay.adapters.base import AdmissionResult, Outcome


def receipt_from(value):
    actor = value['actor']
    return p.Receipt(value['operationId'], value['sessionId'], value['runId'], value['method'], value['payloadDigest'],
        p.Actor(p.ActorKind(actor['kind']), actor['principal'], p.Channel(actor['channel']), actor.get('device')),
        p.ReceiptState(value['state']), value['revision'], p.FailureReason(value['reason']) if value.get('reason') else None)


class NativeInput:
    def __init__(self, runtime):
        self.runtime = runtime
        self.pending = None

    async def register(self, grant, sequence, text):
        from .relay_runtime import operation_from
        r = self.runtime
        async with r.operation_lock:
            if self.pending is not None:
                raise p.RelayError(p.ErrorCode.BUSY)
            handle = (await r.peer.call('native.claude.register', {'grant': grant.grant, 'sequence': sequence,
                'text': text, 'revision': grant.epoch}))['grant']
            await r.stage_grant(handle)
            value = await r.peer.call('native.claude.claim', {'grant': handle})
            staging = value['staging']
            operation = operation_from({**value, 'params': json.loads(r.staging.read(staging['reference'], staging['digest']))})
            if value['origin'] != 'terminal' or value['receiptState'] != 'submitting':
                raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            r.grant = handle
            r.operation_origin[operation.operation_id] = 'terminal'
            fresh = False
            async def permit(op):
                nonlocal fresh
                fresh = True
                self.pending = {'operation': op, 'handle': handle, 'staging': staging, 'removed': False}
                r.active_operation = op.operation_id
                # Journal claim precedes allowing the native hook to return.
                # The existing CLI sends; there is intentionally no SDK send.
                return AdmissionResult(Outcome.UNKNOWN)
            try:
                await r.broker.dispatch(operation, permit)
                if not fresh:
                    raise p.RelayError(p.ErrorCode.INPUT_CONFLICT)
            finally:
                r.operation_origin.pop(operation.operation_id, None)
            return operation

    async def settle(self, evidence):
        r, entry = self.runtime, self.pending
        if entry is None:
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        op = entry['operation']
        if (evidence.kind is not p.EvidenceKind.PROVIDER_ADMISSION
                or (evidence.operation_id, evidence.run_id) != (op.operation_id, op.run_id)):
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        r.guard_check()  # native writer/metadata trust still requires intact frozen guard placement
        r.journal.settle(op, AdmissionResult(Outcome.ACCEPTED, evidence, op.operation_id))
        value = await r.peer.call('native.claude.complete', {'grant': entry['handle'],
            'payloadDigest': op.payload_digest, 'nativeRef': evidence.native_ref})
        receipt = receipt_from(value)
        p.check_registered(receipt, actor=op.actor, session_id=op.session_id, run_id=op.run_id,
                           method=op.method, payload_digest=op.payload_digest)
        if receipt.actor != op.actor or receipt.state is not p.ReceiptState.ACCEPTED:
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        if not entry['removed']:
            r.staging.remove(entry['staging']['reference'], settled=True)
            entry['removed'] = True
        return receipt

    async def processed(self, grant, sequence, operation_id):
        if await self.runtime.peer.call('native.processed', {'grant': grant.grant, 'sequence': sequence,
                'operationId': operation_id}) is not True:
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        self.pending = None
        if self.runtime.active_operation == operation_id:
            self.runtime.active_operation = None


class OwnedAdmission:
    def __init__(self, factory, spec):
        self.factory, self.spec, self.reader = factory, spec, None

    def begin(self):
        from pathlib import Path

        from .claude_admission import NativeAdmission
        if self.reader is None:
            source = self.factory.runtime.native_transcript
            if not source or source[0] != self.spec.run_id:
                raise p.RelayError(p.ErrorCode.UNSUPPORTED)
            env = self.factory.environment(self.spec)
            root = Path(env.get('CLAUDE_CONFIG_DIR', str(Path(env['HOME']) / '.claude'))) / 'projects'
            self.reader = NativeAdmission(root, source[1], self.spec.binding.native_conversation)
        self.reader.begin()

    def evidence(self, operation):
        if self.reader is None:
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        return self.reader.evidence(operation)

    def wait_evidence(self, operation):
        if self.reader is None:
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        return self.reader.wait_evidence(operation)

    def close(self):
        if self.reader:
            self.reader.close()
