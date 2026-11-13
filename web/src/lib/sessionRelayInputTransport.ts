import { RelayClient } from './sessionRelay';
import { operationDigest } from './sessionRelayDigest';
import type { RelayActor, RelayReceipt, RelaySnapshot } from './sessionRelayTypes';
import type { InputTransport, InputWatermark } from './sessionRelayInput';
const actorKey = (actor?: RelayActor | null) => JSON.stringify(actor ? [actor.kind, actor.principal, actor.channel,
  (actor as RelayActor & { device?: string }).device ?? null] : null);
export function inputOwner(snapshot: RelaySnapshot) {
  return snapshot.writer ? { ...snapshot.writer, epoch: snapshot.controlRevision }
    : snapshot.execution ? { ...snapshot.execution, active: snapshot.execution.phase === 'owned', epoch: snapshot.execution.inputEpoch }
    : undefined;
}
export function inputBindingKey(client: RelayClient, snapshot: RelaySnapshot) {
  return JSON.stringify([snapshot.sessionId, snapshot.runId, inputOwner(snapshot)?.epoch, actorKey(inputOwner(snapshot)?.holder), client.attachmentId, client.connectionToken]);
}
/** Freeze the reader/login/attachment identity rather than borrowing a reconnecting client's tokens. */
export function inputTransport(client: RelayClient, snapshot: RelaySnapshot, acceptedClaim?: (receipt: RelayReceipt) => void): InputTransport {
  const attachment = client.attachmentId; const connection = client.connectionToken;
  const runId = snapshot.runId; const owner = inputOwner(snapshot); const epoch = owner?.epoch; const holder = actorKey(owner?.holder);
  if (!attachment || !connection || !runId || owner?.view !== 'terminal' || !owner.mine || !Number.isSafeInteger(epoch)) throw new Error('Input attachment is unavailable');
  const bound = new RelayClient(client.sessionId, client.signal);
  bound.attachmentId = attachment; bound.connectionToken = connection;
  return {
    async claim() {
      const result = await bound.mutate<{ receipt: RelayReceipt; controlRevision: number; holder: RelayActor; inputGrant?: string; inputEpoch?: number }>(
        'control.claim', snapshot, { attachmentId: attachment, expectedRevision: snapshot.controlRevision });
      if (result.receipt?.state !== 'accepted' || result.receipt.method !== 'control.claim'
        || result.receipt.runId !== runId || result.receipt.sessionId !== client.sessionId || actorKey(result.receipt.actor) !== holder
        || actorKey(result.holder) !== holder || result.controlRevision !== epoch || result.inputEpoch !== epoch
        || typeof result.inputGrant !== 'string' || !/^[a-f0-9]{64}$/.test(result.inputGrant)) throw new Error('Input claim is unconfirmed');
      const receipt = result.receipt;
      const digest = await operationDigest('control.claim', { sessionId: client.sessionId, expectedRunId: runId,
        attachmentId: attachment, expectedRevision: snapshot.controlRevision });
      if (!receipt.operationId || !Number.isSafeInteger(receipt.revision) || receipt.revision < 0
          || receipt.payloadDigest !== digest) throw new Error('Input claim receipt is unbound');
      acceptedClaim?.(Object.freeze({ ...receipt, actor: Object.freeze({ ...receipt.actor }) }));
      return { inputGrant: result.inputGrant, inputEpoch: result.inputEpoch };
    },
    write(grant, sequence, data) {
      return bound.call<{ watermark: InputWatermark; partial: boolean }>('terminal.input', {
        attachmentId: attachment, inputGrant: grant, inputEpoch: epoch, frameSeq: sequence, data: btoa(data) });
    },
    drain(grant) { return bound.call<InputWatermark>('terminal.drain', { attachmentId: attachment, inputGrant: grant }); },
    async idle() {
      const current = await bound.snapshot();
      const observed = inputOwner(current);
      if (current.runId !== runId || !observed || observed.epoch !== epoch || !observed.active || !observed.available
        || !observed.mine || observed.view !== 'terminal' || actorKey(observed.holder) !== holder) throw new Error('Input generation ended');
      if (current.execution) {
        const facts = await bound.ownershipRead(runId);
        if (facts.execution?.inputEpoch !== epoch || !facts.execution.mine || !facts.execution.available) throw new Error('Input generation ended');
        if (facts.selector?.idle_verified !== true) return false;
      }
      return !current.activeTurnId && !current.pendingDecisions.some(decision => ['pending', 'reserved', 'outcome_unknown'].includes(decision.status))
        && !current.receipts.some(receipt => ['queued', 'submitting', 'uncertain'].includes(receipt.state));
    },
  };
}

/** Only newer evidence for this exact claim supersedes a pending poll result. */
export function acceptedClaimSupersedes(accepted: RelayReceipt | undefined, observed: RelayReceipt) {
  return !!accepted && accepted.state === 'accepted' && accepted.method === 'control.claim'
    && observed.method === accepted.method && observed.operationId === accepted.operationId
    && observed.sessionId === accepted.sessionId && observed.runId === accepted.runId
    && observed.payloadDigest === accepted.payloadDigest && actorKey(observed.actor) === actorKey(accepted.actor)
    && Number.isSafeInteger(observed.revision) && observed.revision >= 0 && accepted.revision > observed.revision;
}
