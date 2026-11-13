import { operationDigest } from './sessionRelayDigest';
import type { RelaySnapshot } from './sessionRelayTypes';

/** A previous chosen answer exempts only its exact registered receipt. */
export async function knownDecisionReceipts(snapshot: RelaySnapshot): Promise<Set<string>> {
  const known = new Set<string>();
  for (const decision of [...snapshot.pendingDecisions, ...(snapshot.decisionOutcomes ?? [])]) {
    if (!['reserved', 'outcome_unknown'].includes(decision.status) || !decision.choiceId || !decision.winner
        || decision.runId !== snapshot.runId) continue;
    const revision = decision.revision - (decision.status === 'reserved' ? 1 : 2);
    if (revision < 0) continue;
    const digest = await operationDigest('decision.reply', { sessionId: snapshot.sessionId,
      expectedRunId: decision.runId, decisionId: decision.decisionId, choiceId: decision.choiceId,
      expectedRevision: revision, actionDigest: decision.actionDigest });
    for (const receipt of snapshot.receipts) {
      if (receipt.method === 'decision.reply' && receipt.sessionId === snapshot.sessionId
          && receipt.runId === decision.runId && receipt.payloadDigest === digest
          && receipt.actor.kind === decision.winner.kind && receipt.actor.principal === decision.winner.principal
          && receipt.actor.channel === decision.winner.channel) known.add(receipt.operationId);
    }
  }
  return known;
}
